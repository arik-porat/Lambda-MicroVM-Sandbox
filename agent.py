"""
Strands agent that completes a task in a Lambda MicroVM sandbox.

The script creates a sandbox (a new MicroVM from the command-runner image), lets the agent run bash on it
through its run_command tool until the task is done, then terminates the sandbox, also on errors or Ctrl+C.

    python3 agent.py "Write a Python script that prints the first 20 primes and run it"

Requires `pip install strands-agents "boto3>=1.43.35"`, Bedrock model access, and the command-runner
image built from this folder (build_image.py).
"""

import argparse
import sys

from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor

from sandbox import DEFAULT_PORT, Sandbox

MAX_OUTPUT = 20_000  # chars of command output sent back to the model

SYSTEM_PROMPT = """You complete tasks on a Linux MicroVM using the run_command tool.
Work in small steps and check each command's exit code and output. When something fails, fix the cause and retry.
Verify the result before you finish. Don't ask the user questions; make reasonable assumptions.
Don't stop processes you didn't start: one of them runs your commands.
When done, reply with a short summary of what you did, where the results are, and how you verified them."""


def make_run_command(sandbox: Sandbox):
    @tool
    def run_command(command: str, timeout: int = 120) -> str:
        """Run a bash script on the MicroVM and return its exit code and combined stdout/stderr.

        Each call starts a new bash process: files persist between calls, but the working directory
        and environment variables do not. Stdin is /dev/null, so use non-interactive flags
        (for example `dnf install -y`). Run long-lived processes such as servers in the background
        with nohup and redirect their output to a log file (`nohup cmd > app.log 2>&1 &`).

        Args:
            command: Bash script to run. Multi-line scripts are fine.
            timeout: Seconds to wait for the script to finish.
        """
        print(f"\n$ {command}", file=sys.stderr)
        r = sandbox.run(command, timeout)
        out = r.output + (f"\n[timed out after {timeout}s]" if r.timed_out else "")
        print(f"{out}[exit {r.exit_code}]", file=sys.stderr)
        return f"exit_code: {r.exit_code}\n{out[-MAX_OUTPUT:]}"

    return run_command


def main() -> None:
    ap = argparse.ArgumentParser(description="Complete a task in a Lambda MicroVM sandbox with a Strands agent")
    ap.add_argument("--image", default="command-runner", help="MicroVM image name or ARN (default: command-runner)")
    ap.add_argument("--model-id", default=None, help="Bedrock model id (default: the Strands default model)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="port the image's command server listens on (default: %(default)s)")
    ap.add_argument("task", help="what the agent should do in the sandbox")
    args = ap.parse_args()

    with Sandbox.create(image=args.image, port=args.port) as sandbox:  # terminated when the block exits
        agent = Agent(
            model=args.model_id,
            system_prompt=SYSTEM_PROMPT,
            tools=[make_run_command(sandbox)],
            tool_executor=SequentialToolExecutor(),  # commands depend on each other, so run them in order
        )
        agent(args.task)
        print()


if __name__ == "__main__":
    main()
