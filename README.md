# Lambda-MicroVM-Sandbox

Example code that shows how your AI agents can use AWS Lambda MicroVMs as a sandbox to run code.

The agent runs on your machine. When it needs to run a command, the command goes to a disposable
MicroVM. The code the agent writes, the packages it installs and any mistakes it makes stay inside
that MicroVM, which is terminated when the task is done.

The example has three parts:

- **Lambda MicroVM image** (`Dockerfile`, `server.py`, `build_image.py`): an image with a small command
  server that runs bash scripts sent to it over HTTPS.
- **Sandbox client** (`sandbox.py`): a Python `Sandbox` class that launches a MicroVM from the image,
  runs commands on it and terminates it.
- **Agent** (`agent.py`): a [Strands](https://strandsagents.com) agent whose `run_command` tool runs
  each command in a sandbox.

The client works with any agent framework. Wrap `sandbox.run()` in a tool the same way `agent.py` does.

```python
from sandbox import Sandbox

with Sandbox.create() as sandbox:  # terminated when the block exits, even on an exception
    result = sandbox.run('echo "Hello from Sandbox!"')
    print(result.output, result.exit_code)
```

## How it works

```
your code ── sandbox.py ──HTTPS (HTTP_INGRESS)──▶ Lambda proxy ──▶ MicroVM
              │  X-aws-proxy-auth: <token>         checks token     server.py :8900  runs bash
              │  X-aws-proxy-port: 8900                             server.py :8901  lifecycle hooks
              └─ boto3 lambda-microvms: RunMicrovm, CreateMicrovmAuthToken, TerminateMicrovm
```

1. `build_image.py` zips `Dockerfile` and `server.py`, uploads the zip to S3, and creates (or updates) a
   MicroVM image named `command-runner`. Lambda builds it on the managed AL2023 base image, starts
   `server.py`, and snapshots the VM once the server answers the `/ready` hook.
2. `Sandbox.create()` calls `RunMicrovm` with the `HTTP_INGRESS` connector (and `INTERNET_EGRESS` unless
   you pass `internet=False`) and waits for the MicroVM to reach `RUNNING`.
3. `Sandbox.run()` gets a short-lived auth token scoped to port 8900, posts the script to `POST /exec`,
   and polls `GET /exec/<id>` until it finishes. No request stays open longer than about 25 seconds, so
   commands can run for hours.
4. `Sandbox.kill()` calls `TerminateMicrovm`. If your process dies first, Lambda still terminates the
   MicroVM when its `lifetime` runs out.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `Dockerfile` | build | Base image `public.ecr.aws/lambda/microvms:al2023-minimal` plus python3 and `server.py`. |
| `server.py` | MicroVM | Command server. Runs bash scripts as background jobs (port 8900) and answers Lambda lifecycle hooks (port 8901). Standard library only. |
| `build_image.py` | your machine | Packages the build context, creates a least-privilege build role, and creates or updates the MicroVM image. |
| `sandbox.py` | your machine | `Sandbox` client: create, run, kill. |
| `agent.py` | your machine | Strands agent with a `run_command` tool that executes in a sandbox. |

## Requirements

- Python 3.10 or later on your machine.
- `boto3>=1.43.35`. Older versions have no `lambda-microvms` client.
- `strands-agents` and Amazon Bedrock model access, for `agent.py` only.
- AWS credentials allowed to call the Lambda MicroVMs API, write to the S3 bucket, and create and pass
  an IAM role.
- An existing S3 bucket in the same region for the build artifact. `build_image.py` does not create it.

The region comes from `AWS_REGION` (default `us-east-1`). The image is built for `ARM_64`.

```bash
pip install "boto3>=1.43.35" strands-agents
```

## Quick start

### 1. Build the image

```bash
python3 build_image.py                                          # uses the bucket lambdamv-images-<your account id>
python3 build_image.py --bucket <your-bucket>                   # or any other existing bucket
python3 build_image.py --name command-runner-8gb --memory 8192  # a second image with 8 GiB of memory
```

The bucket must already exist. The script prints the build state every 15 seconds until the version is
`SUCCESSFUL`. Run it again after you change `server.py` or `Dockerfile` to build a new image version.

Memory is set on the image, not in `Sandbox.create()`. For a bigger sandbox, build a second image like
the last command does and launch it by name with `Sandbox.create(image="command-runner-8gb")`.

| Option | Default | Meaning |
|---|---|---|
| `--name` | `command-runner` | Image name. Also used for the build role (`<name>-build-role`) and the log group (`/aws/lambda-microvms/<name>`). |
| `--bucket` | `lambdamv-images-<account id>` | Existing S3 bucket for the build context zip. The account ID is the caller's, from `sts get-caller-identity`. |
| `--key` | `<name>/app.zip` | S3 key for the zip. |
| `--memory` | `4096` | Minimum memory in MiB. |
| `--no-wait` | off | Submit the build and exit without polling. |

### 2. Run an agent

```bash
python3 agent.py "Write a Python script that prints the first 20 primes and run it"
```

Each command and its output are printed to stderr as the agent works. The agent's final summary goes to
stdout. Options: `--image` (name or ARN), `--model-id` (Bedrock model ID, default is the Strands default
model) and `--port`.

## Python API

```python
Sandbox.create(
    image="command-runner",     # image name or ARN
    lifetime=3600,              # seconds until Lambda terminates the MicroVM (max 28800)
    internet=True,              # attach INTERNET_EGRESS so commands can reach the internet
    execution_role_arn=None,    # IAM role for AWS credentials inside the MicroVM
                                # (default: $MICROVM_EXECUTION_ROLE_ARN, or none)
    port=8900,                  # port of the image's command server
) -> Sandbox

sandbox.run(command, timeout=60) -> Result   # Result(exit_code, output, timed_out)
sandbox.kill()                               # terminate; safe to call more than once
```

- Memory (`--memory`) and CPU architecture (`ARM_64`) are set when the image is built. The `RunMicrovm`
  API has no parameter for them, so pick a different image to change them.
- Each `run()` starts a new `bash -c` process in `$HOME`. Files persist between calls; the working
  directory and environment variables do not.
- Stdin is `/dev/null`, so use non-interactive flags such as `dnf install -y`.
- `output` is stdout and stderr combined. If it exceeds 1 MiB, only the last 1 MiB is kept.
- A command that exceeds `timeout` is killed together with every process it started, and returns exit
  code 124 with `timed_out=True`.
- Background processes started with `nohup cmd > log 2>&1 &` keep running after the call returns.
- The client renews its 15-minute auth token automatically and retries throttling, 5xx responses and
  dropped connections with backoff. Job IDs are chosen by the client, so a retried `POST` never runs a
  command twice.
- MicroVMs are launched with an idle policy (`maxIdleDurationSeconds: 900`,
  `suspendedDurationSeconds: 300`, `autoResumeEnabled: true`), so an idle sandbox is suspended and
  resumed by the next request.

## Command server API

`server.py` listens on port 8900 behind the Lambda HTTPS proxy. Every request needs the
`X-aws-proxy-auth` token and `X-aws-proxy-port: 8900` headers.

| Request | Response |
|---|---|
| `POST /exec` with `{"id": "<id>", "command": "<bash script>", "timeout": 120}` | `202 {"id": "<id>"}`. The job starts in the background. Posting an existing ID returns that job without starting a new one. |
| `GET /exec/<id>?wait=20` | `200 {"done": false}` if still running after `wait` seconds (capped at 25), or `200 {"done": true, "exit_code": 0, "output": "...", "timed_out": false}`. |
| `GET /health` | `200 {"ok": true}` |

IDs match `[A-Za-z0-9_-]{1,64}`. `timeout` must be between 1 and 28800 seconds. Finished results are kept
for 10 minutes so a client that lost a response can fetch it again.

Port 8901 answers Lambda's lifecycle hooks under `/aws/lambda-microvms/runtime/v1/`. It is separate from
the command port so caller tokens cannot reach it.

## Security notes

- `server.py` does no authentication. Lambda's proxy checks the token, so only issue tokens whose
  `allowedPorts` contains port 8900 alone, as `sandbox.py` does.
- Anyone holding a valid token can run arbitrary commands in that MicroVM. Tokens expire after 15
  minutes.
- The image is built with `additionalOsCapabilities: ["ALL"]` so commands can run `dockerd`, mount
  filesystems and so on. These capabilities apply inside the MicroVM's isolation boundary.
- With `internet=True`, commands have outbound internet access. Use `internet=False` for untrusted code
  that should not reach the network.
- Only pass an `execution_role_arn` with the permissions the sandboxed code needs, since every command
  in the MicroVM can use those credentials.
- The build role created by `build_image.py` can only read the build artifact and write to the image's
  log group.

## Troubleshooting

- **Build fails:** `build_image.py` prints each build's state reason. Full logs are in the CloudWatch log
  group `/aws/lambda-microvms/<name>`.
- **`no MicroVM image named 'command-runner'`:** build the image first, in the same region as
  `AWS_REGION`.
- **`403 Forbidden. Was the MicroVM launched with HTTP_INGRESS?`:** the MicroVM has no ingress connector,
  or the token is not valid for the port in `X-aws-proxy-port`.
- **`UnknownServiceError: lambda-microvms`:** upgrade boto3 to 1.43.35 or later.
