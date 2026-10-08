# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2"]
# ///
"""Measure one disposable OpenShell workload for spike #1797, without suite conformance claims."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import time
import uuid
from importlib import import_module


def main() -> int:
    """Create a uniquely named fixture, measure execution, and always attempt its deletion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True, help="Local image ID or digest-qualified image")
    parser.add_argument("--endpoint", default="127.0.0.1:19797")
    parser.add_argument("--observe-seconds", type=int, default=0, choices=range(0, 31))
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be immutable")
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", args.endpoint):
        parser.error("this plaintext local fixture requires an IPv4 loopback endpoint")
    if importlib.metadata.version("openshell") != "0.1.2":
        raise RuntimeError("this probe requires openshell 0.1.2")

    sdk = import_module("openshell.sandbox")
    pb = import_module("openshell._proto.openshell_pb2")
    policy_pb = import_module("openshell._proto.sandbox_pb2")
    name = "s1797-" + uuid.uuid4().hex[:12]
    report = {"name": name, "scope": "real Docker workload; no suite adapter", "image": args.image}
    client = sdk.SandboxClient(args.endpoint, timeout=20)
    policy = policy_pb.SandboxPolicy(version=1)
    policy.filesystem.read_only.extend(["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc"])
    policy.filesystem.read_write.extend(["/sandbox", "/tmp", "/home/sandbox", "/dev/null"])
    policy.filesystem.include_workdir = True
    policy.landlock.compatibility = "hard_requirement"
    policy.process.run_as_user = "sandbox"
    policy.process.run_as_group = "sandbox"
    spec = pb.SandboxSpec(policy=policy, command=["sleep", "3600"])
    spec.template.image = args.image
    spec.template.labels["spike.issue"] = "1797"

    def execute(code: str, timeout: int = 10) -> dict[str, object]:
        start = time.monotonic()
        chunks = list(
            client.exec_stream(
                name,
                ["python", "-c", code],
                workspace="default",
                workdir="/sandbox",
                timeout_seconds=timeout,
                no_login_shell=True,
            )
        )
        return {
            "seconds": time.monotonic() - start,
            "exit_code": chunks[-1].exit_code,
            "stdout_hex": b"".join(
                x.data for x in chunks if isinstance(x, sdk.ExecChunk) and x.stream == "stdout"
            ).hex(),
            "stderr_hex": b"".join(
                x.data for x in chunks if isinstance(x, sdk.ExecChunk) and x.stream == "stderr"
            ).hex(),
        }

    try:
        report["gateway_version"] = client.health().version
        if report["gateway_version"] != "0.1.2":
            raise RuntimeError("this probe requires gateway 0.1.2")
        sandbox = client.create(name=name, workspace="default", spec=spec)
        report["sandbox_id"] = sandbox.id
        client.wait_ready(name, workspace="default", timeout_seconds=60)
        report["ready"] = True
        report["bytes"] = execute(
            "import os; os.write(1,b'\\x00\\xff\\xfe\\x80'); os.write(2,b'\\xfe\\x00\\xff')"
        )
        report["natural_124"] = execute("raise SystemExit(124)")
        report["timeout"] = execute("import time; time.sleep(10)", timeout=1)
        child_code = (
            "import time,pathlib; time.sleep(3); "
            "pathlib.Path('/sandbox/child-marker').write_text('survived'); time.sleep(60)"
        )
        report["timeout_with_child"] = execute(
            "import subprocess,time; subprocess.Popen(['python','-c',"
            + repr(child_code)
            + "]); time.sleep(10)",
            timeout=1,
        )
        time.sleep(4)
        report["child_marker"] = execute(
            "from pathlib import Path; print(Path('/sandbox/child-marker').exists())"
        )
        report["closed_direct_tcp"] = execute(
            "import socket; s=socket.socket(); s.settimeout(2); "
            "print(s.connect_ex(('1.1.1.1',443)))"
        )
        if args.observe_seconds:
            print(json.dumps({"observation_window": name, "sandbox_id": sandbox.id}), flush=True)
            time.sleep(args.observe_seconds)
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    finally:
        try:
            deleted = client.delete(name, workspace="default", allow_missing=True)
            report["delete_outcome"] = str(deleted.outcome)
            client.wait_deleted(
                name,
                workspace="default",
                expected_sandbox_id=deleted.sandbox_id,
                timeout_seconds=30,
            )
            report["api_deleted"] = True
        except Exception as exc:
            report["cleanup_error"] = {"type": type(exc).__name__, "detail": str(exc)}
        client.close()
        print(json.dumps(report, indent=2), flush=True)
    return int("error" in report or "cleanup_error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
