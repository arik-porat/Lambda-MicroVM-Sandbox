"""
Package this folder (Dockerfile + server.py) and build it as a Lambda MicroVM image.

    python3 build_image.py                  # create the image, or a new version if it exists
    python3 build_image.py --bucket my-bkt  # use another existing S3 bucket for the build context
    python3 build_image.py --no-wait        # submit and return

Steps:
  1. Zip the build context (Dockerfile at the zip root) and upload it to S3.
  2. Ensure a build role scoped to this image's artifact and log group.
  3. CreateMicrovmImage (first run) or UpdateMicrovmImage (later runs).
  4. Poll until the version succeeds or fails.

Then try it with `python3 sandbox.py`, which launches a MicroVM from it, runs one command and terminates it.

Requires "boto3>=1.43.35" (older boto3 has no lambda-microvms model).
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["Dockerfile", "server.py"]  # build context; the Dockerfile must be at the zip root
BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INTERNET_EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
HOOK_PORT = 8901  # server.py's hook port


def package(bucket: str, key: str) -> str:
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(os.path.join(HERE, f), arcname=f)
    boto3.client("s3", region_name=REGION).put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
    print(f"[package] {', '.join(FILES)} -> s3://{bucket}/{key} ({buf.tell()} bytes)")
    return f"s3://{bucket}/{key}"


def ensure_build_role(name: str, account: str, bucket: str, key: str, log_group: str) -> str:
    """Role Lambda assumes during the build: read the artifact, write build logs. Nothing else."""
    iam = boto3.client("iam")
    role_name = f"{name}-build-role"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnEquals": {"aws:SourceArn": [
                    BASE_IMAGE_ARN,
                    f"arn:aws:lambda:{REGION}:{account}:microvm-image:{name}",
                ]},
            },
        }],
    }
    log_arn = f"arn:aws:logs:{REGION}:{account}:log-group:{log_group}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/{key}",
             "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"{log_arn}:log-stream:*"},
            {"Effect": "Allow", "Action": "logs:CreateLogGroup", "Resource": f"{log_arn}:*"},
        ],
    }

    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust))
        print(f"[role] reusing {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=role_name, Path="/service-role/",
                              AssumeRolePolicyDocument=json.dumps(trust),
                              Description=f"Build role for Lambda MicroVM image {name}")["Role"]["Arn"]
        print(f"[role] created {arn}")
        time.sleep(10)  # let IAM propagate before Lambda tries to assume it
    iam.put_role_policy(RoleName=role_name, PolicyName="microvm-image-build", PolicyDocument=json.dumps(policy))
    return arn


def find_image(client, name: str) -> dict | None:
    for page in client.get_paginator("list_microvm_images").paginate():
        for item in page.get("items", []):
            if item["name"] == name:
                return item
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="command-runner")
    ap.add_argument("--bucket", default=None,
                    help="existing S3 bucket for the build context (default: lambdamv-images-<account id>)")
    ap.add_argument("--key", default=None, help="default: <name>/app.zip")
    ap.add_argument("--memory", type=int, default=4096, help="minimum memory in MiB")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    key = args.key or f"{args.name}/app.zip"
    log_group = f"/aws/lambda-microvms/{args.name}"
    account = boto3.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"lambdamv-images-{account}"
    client = boto3.client("lambda-microvms", region_name=REGION)

    uri = package(bucket, key)
    role_arn = ensure_build_role(args.name, account, bucket, key, log_group)

    config = {
        "baseImageArn": BASE_IMAGE_ARN,
        "buildRoleArn": role_arn,
        "codeArtifact": {"uri": uri},
        "description": "Command runner: server.py runs bash commands sent over HTTP_INGRESS",
        "logging": {"cloudWatch": {"logGroup": log_group}},
        # The build installs python3, and the agent's commands install packages and pull images.
        "egressNetworkConnectors": [INTERNET_EGRESS],
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "resources": [{"minimumMemoryInMiB": args.memory}],
        # Lets commands run dockerd, mount filesystems, etc. Capabilities apply inside the VM isolation boundary only.
        "additionalOsCapabilities": ["ALL"],
        # server.py answers /ready on its own hook port once its command port is listening; Lambda then snapshots.
        "hooks": {
            "port": HOOK_PORT,
            "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 120},
        },
    }

    existing = find_image(client, args.name)

    if existing:
        r = client.update_microvm_image(imageIdentifier=existing["imageArn"], **config)
        print(f"[image] updating {r['imageArn']} -> version {r['imageVersion']}")
    else:
        r = client.create_microvm_image(name=args.name, **config)
        print(f"[image] creating {r['imageArn']} version {r['imageVersion']}")
    image_arn, version = r["imageArn"], r["imageVersion"]

    if args.no_wait:
        return 0

    # No waiter in the model yet; poll the version.
    start = time.time()

    while time.time() - start < 3600:
        v = client.get_microvm_image_version(imageIdentifier=image_arn, imageVersion=version)
        print(f"[image] {int(time.time() - start):>4}s state={v['state']} status={v.get('status')}")

        if v["state"] == "SUCCESSFUL":
            print(f"\nImage ready: {image_arn} version {version}")
            return 0

        if v["state"] in ("FAILED", "CREATE_FAILED", "UPDATE_FAILED"):
            builds = client.list_microvm_image_builds(imageIdentifier=image_arn, imageVersion=version)["items"]

            for b in builds:
                print(f"  build {b['buildId']}: {b['buildState']} {b.get('stateReason', '')}")
            print(f"Build logs: CloudWatch log group {log_group}", file=sys.stderr)
            return 1
        time.sleep(15)
    print("timed out waiting for the build", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
