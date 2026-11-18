"""
Command server that runs inside the MicroVM and executes bash scripts sent through HTTP_INGRESS.

Clients call the MicroVM's HTTPS endpoint with an X-aws-proxy-auth token and `X-aws-proxy-port: 8900`.
Lambda checks the token and forwards the request here. This server does no authentication of its own,
so only issue tokens whose allowedPorts is EXEC_PORT.

    POST /exec              {"id": "<client-chosen id>", "command": "<bash script>", "timeout": 120}
                            -> 202 {"id": ...}. Posting an id again returns the existing job, so retries are safe.
    GET  /exec/<id>?wait=20 -> 200 {"done": false} if still running after waiting up to `wait` seconds
                            -> 200 {"done": true, "exit_code": 0, "output": "...", "timed_out": false}
    GET  /health            -> 200 {"ok": true}

Commands run in the background and the client polls for the result, so no request stays open longer
than POLL_MAX seconds however long the command runs.

HOOK_PORT answers Lambda's lifecycle hooks (/aws/lambda-microvms/runtime/v1/*). It is a separate port
so tokens issued to callers can't reach the hooks.

Python 3.9 standard library only (AL2023's python3).
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

EXEC_PORT = 8900
HOOK_PORT = 8901  # must match hooks.port in build_image.py
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
MAX_OUTPUT = 1 << 20  # bytes of output returned per command; longer output keeps the tail
MAX_TIMEOUT = 8 * 3600  # a MicroVM lives at most 8 hours
POLL_MAX = 25  # longest a GET waits before answering {"done": false}
KEEP_RESULTS = 600  # seconds a finished job's result stays available, so a lost response can be fetched again
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
WORKDIR = os.environ.get("HOME") or "/"

jobs = {}
jobs_lock = threading.Lock()


class Job:
    def __init__(self, command: str, timeout: int):
        self.command = command
        self.timeout = timeout
        self.done = threading.Event()
        self.result = None
        self.finished_at = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(job_id: str, job: Job) -> None:
    started = time.monotonic()
    timed_out = False

    try:
        # Output goes to a file, not a pipe: a background process (`nohup server &`) would hold a pipe open
        # and the call would wait for it. A new session puts the script and everything it starts in one
        # process group, so a timeout kills all of it.
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(["bash", "-c", job.command], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, cwd=WORKDIR, start_new_session=True)
            try:
                rc = proc.wait(timeout=job.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                rc = 124  # what coreutils `timeout` returns

            if rc < 0:  # killed by a signal: report it the way a shell does
                rc = 128 - rc
            size = out.seek(0, os.SEEK_END)
            out.seek(max(0, size - MAX_OUTPUT))
            output = out.read().decode(errors="replace")

            if size > MAX_OUTPUT:
                output = f"[first {size - MAX_OUTPUT} bytes of output dropped]\n{output}"
    except Exception as e:  # e.g. bash missing; report it instead of leaving the job running forever
        rc, output = 1, f"[command server error: {e!r}]\n"

    with jobs_lock:
        job.result = {"done": True, "exit_code": rc, "output": output, "timed_out": timed_out}
        job.finished_at = time.monotonic()
    job.done.set()
    log(f"job {job_id} exit {rc} after {time.monotonic() - started:.1f}s")


def prune() -> None:
    """Drop finished jobs older than KEEP_RESULTS. Caller holds jobs_lock."""
    now = time.monotonic()

    for job_id in [j for j, job in jobs.items() if job.finished_at and now - job.finished_at > KEEP_RESULTS]:
        del jobs[job_id]


class ExecHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive with the Lambda proxy; every reply sets Content-Length

    def do_POST(self) -> None:
        if self.path != "/exec":
            return self.reply(404, {"error": "not found"})

        try:
            body = json.loads(self.read_body())
            job_id, command, timeout = body["id"], body["command"], int(body.get("timeout", 120))
            valid = bool(JOB_ID.fullmatch(job_id)) and isinstance(command, str) and 1 <= timeout <= MAX_TIMEOUT
        except (ValueError, KeyError, TypeError):
            valid = False

        if not valid:
            return self.reply(400, {"error": f"expected JSON {{id, command, timeout (1-{MAX_TIMEOUT})}}"})

        with jobs_lock:
            prune()
            job = jobs.get(job_id)
            is_new = job is None

            if is_new:
                job = jobs[job_id] = Job(command, timeout)

        if is_new:
            log(f"job {job_id} started (timeout {timeout}s)")
            threading.Thread(target=run, args=(job_id, job), daemon=True).start()
        self.reply(202, {"id": job_id})

    def do_GET(self) -> None:
        url = urlsplit(self.path)

        if url.path == "/health":
            return self.reply(200, {"ok": True})
        m = re.fullmatch(r"/exec/([A-Za-z0-9_-]{1,64})", url.path)

        with jobs_lock:
            job = jobs.get(m.group(1)) if m else None

        if not job:
            return self.reply(404, {"error": "no such job"})

        try:
            wait = min(max(float(parse_qs(url.query).get("wait", ["0"])[0]), 0), POLL_MAX)
        except ValueError:
            return self.reply(400, {"error": "wait must be a number of seconds"})
        job.done.wait(wait)
        self.reply(200, job.result if job.done.is_set() else {"done": False})

    def read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = b""

            while size := int(self.rfile.readline().split(b";")[0], 16):
                data += self.rfile.read(size)
                self.rfile.readline()  # CRLF after each chunk

            while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # optional trailers
                pass
            return data
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-") -> None:
        pass  # polling would flood the log; run() logs each job and errors are still logged


class HookHandler(BaseHTTPRequestHandler):
    """Lambda's lifecycle hooks: /ready during the image build; /run, /suspend, ... at run time if enabled.
    This server starts after the exec server is listening, so answering /ready means the image is ready."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200 if self.path.startswith(HOOK_PREFIX) else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_request(self, code="-", size="-") -> None:
        # Lambda first probes the port with TLS; that request never gets a path.
        log(f"hook {getattr(self, 'path', '(unparsed request)')} -> {code}")


def main() -> None:
    exec_server = ThreadingHTTPServer(("0.0.0.0", EXEC_PORT), ExecHandler)
    hook_server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler)
    threading.Thread(target=hook_server.serve_forever, daemon=True).start()
    log(f"command server: exec on {EXEC_PORT}, hooks on {HOOK_PORT}")
    exec_server.serve_forever()


if __name__ == "__main__":
    main()
