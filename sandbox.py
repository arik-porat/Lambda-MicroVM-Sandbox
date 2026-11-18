"""
Simple sandbox API on Lambda MicroVMs.

    from sandbox import Sandbox

    sandbox = Sandbox.create()
    result = sandbox.run('echo "Hello from Sandbox!"')
    print(result.output, result.exit_code)
    sandbox.kill()

`with Sandbox.create() as sandbox:` kills the sandbox when the block exits, even on an exception.

Sandbox.create() launches a MicroVM from the command-runner image (build it with build_image.py) and waits
until it is RUNNING. kill() terminates the MicroVM. If this process dies before kill() runs, Lambda still
terminates the MicroVM after `lifetime` seconds.

run() sends the command to the image's command server through the MicroVM's HTTPS endpoint (HTTP_INGRESS):
POST /exec starts it in the background and GET /exec/<id> polls for its result, so long commands never hold
one request open. Requests carry an auth token scoped to the command server's port only (X-aws-proxy-auth)
and select that port with X-aws-proxy-port. The port is DEFAULT_PORT unless you pass Sandbox.create(port=...).

    python3 sandbox.py      # create a sandbox, run one command, kill it

Requires "boto3>=1.43.35" (older boto3 has no lambda-microvms model).
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

import boto3

DEFAULT_PORT = 8900  # the command-runner image's command port. Tokens are scoped to it only.
REGION = os.environ.get("AWS_REGION", "us-east-1")
HTTP_INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS"
INTERNET_EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
START_TIMEOUT = 300  # seconds to wait for PENDING -> RUNNING
TOKEN_MINUTES = 15  # renewed before it expires, so commands can run longer than this
POLL_WAIT = 20  # seconds each result request waits on the MicroVM before answering "still running"
RETRY_STATUS = {429, 500, 502, 503, 504}  # endpoint errors worth retrying (502 includes a failed auto-resume)
client = boto3.client("lambda-microvms", region_name=REGION)


class MicrovmError(Exception):
    pass


@dataclass
class Result:
    exit_code: int
    output: str  # stdout and stderr combined
    timed_out: bool


class Sandbox:
    def __init__(self, microvm_id: str, endpoint: str, port: int = DEFAULT_PORT):
        self.id = microvm_id
        self._base_url = f"https://{endpoint}"
        self._port = port
        self._token = None
        self._token_expires = 0.0

    @classmethod
    def create(cls, image: str = "command-runner", lifetime: int = 3600, internet: bool = True,
               execution_role_arn: str | None = None, port: int = DEFAULT_PORT) -> "Sandbox":
        """Launch a MicroVM and wait until it is RUNNING.

        image: image name or ARN. lifetime: seconds until Lambda terminates the MicroVM (max 28800).
        internet: give commands outbound internet access. execution_role_arn: IAM role for AWS
        credentials inside the MicroVM (default: $MICROVM_EXECUTION_ROLE_ARN, or none).
        port: port the image's command server listens on.
        """
        kwargs = {
            "imageIdentifier": image if image.startswith("arn:") else image_arn(image),
            "ingressNetworkConnectors": [HTTP_INGRESS],
            "idlePolicy": {"maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 300, "autoResumeEnabled": True},
            "maximumDurationInSeconds": lifetime,
        }

        if internet:
            kwargs["egressNetworkConnectors"] = [INTERNET_EGRESS]
        role = execution_role_arn or os.environ.get("MICROVM_EXECUTION_ROLE_ARN")

        if role:
            kwargs["executionRoleArn"] = role
        d = client.run_microvm(**kwargs)
        microvm_id = d["microvmId"]
        log(f"[sandbox] {microvm_id} starting")

        try:
            deadline = time.monotonic() + START_TIMEOUT

            while d["state"] == "PENDING" and time.monotonic() < deadline:
                time.sleep(1)
                d = client.get_microvm(microvmIdentifier=microvm_id)

            if d["state"] != "RUNNING" or not d.get("endpoint"):
                raise MicrovmError(f"{microvm_id} is {d['state']}, expected RUNNING. {d.get('stateReason', '')}")
        except BaseException:  # includes Ctrl+C: don't leave a MicroVM behind
            terminate(microvm_id)
            raise
        log(f"[sandbox] {microvm_id} running")
        return cls(microvm_id, d["endpoint"], port)

    def run(self, command: str, timeout: int = 60) -> Result:
        """Run a bash script and wait for it. The MicroVM kills it after `timeout` seconds (exit code 124)."""
        job_id = uuid.uuid4().hex  # chosen here so a retried POST can't start the command twice
        self._request("POST", "/exec", {"id": job_id, "command": command, "timeout": timeout})
        deadline = time.monotonic() + timeout + 60

        while time.monotonic() < deadline:
            r = self._request("GET", f"/exec/{job_id}?wait={POLL_WAIT}", timeout=POLL_WAIT + 30)

            if r["done"]:
                return Result(exit_code=r["exit_code"], output=r["output"], timed_out=r["timed_out"])
        raise MicrovmError(f"no result from the MicroVM {timeout + 60}s after starting the command")

    def kill(self) -> None:
        """Terminate the MicroVM. Safe to call more than once."""
        terminate(self.id)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.kill()

    def _auth_token(self, renew: bool = False) -> str:
        if renew or time.time() > self._token_expires - 60:
            d = client.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=TOKEN_MINUTES,
                                                 allowedPorts=[{"port": self._port}])
            self._token = d["authToken"]["X-aws-proxy-auth"]
            self._token_expires = time.time() + TOKEN_MINUTES * 60
        return self._token

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        """Send one JSON request to the command server, retrying throttling, endpoint errors and dropped connections."""
        data = json.dumps(body).encode() if body is not None else None
        renew = renewed = False

        for attempt in range(6):
            headers = {"X-aws-proxy-auth": self._auth_token(renew), "X-aws-proxy-port": str(self._port),
                       "Content-Type": "application/json"}
            req = urllib.request.Request(self._base_url + path, data=data, method=method, headers=headers)
            renew = False

            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode(errors="replace")

                if e.code == 403 and not renewed:  # token expired or revoked: get a new one and retry once
                    renew = renewed = True
                    continue

                if e.code == 403:
                    raise MicrovmError(f"{method} {path}: 403 Forbidden. Was the MicroVM launched with HTTP_INGRESS? {detail}")

                if e.code not in RETRY_STATUS:
                    raise MicrovmError(f"{method} {path}: HTTP {e.code} {detail}")
                error = f"HTTP {e.code} {detail}"
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                error = repr(e)
            time.sleep(min(2 ** attempt, 15))
        raise MicrovmError(f"{method} {path} failed after retries: {error}")


def image_arn(name: str) -> str:
    for page in client.get_paginator("list_microvm_images").paginate():
        for item in page.get("items", []):
            if item["name"] == name:
                return item["imageArn"]
    raise MicrovmError(f"no MicroVM image named {name!r} in {REGION}; build it with build_image.py")


def terminate(microvm_id: str) -> None:
    client.terminate_microvm(microvmIdentifier=microvm_id)  # idempotent
    log(f"[sandbox] {microvm_id} terminated")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    sandbox = Sandbox.create()

    try:
        result = sandbox.run('echo "Hello from Sandbox!"')
        print(result.output, end="")
        print(f"exit code: {result.exit_code}")
    finally:
        sandbox.kill()
