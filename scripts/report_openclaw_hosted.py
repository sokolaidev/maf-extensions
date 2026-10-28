"""Publish bounded sample evidence without retaining private Gateway state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    """Identify retained bytes without publishing their contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*args: str) -> str:
    """Read a bounded local runtime observation."""
    return subprocess.check_output(args, text=True, timeout=30, stderr=subprocess.DEVNULL).strip()


def observations(report: dict[str, Any], mode: str = "smoke") -> tuple[dict[str, Any], bool]:
    """Require the requested profile, observed compiler results and complete cleanup."""
    flags = ("passed", "owned_empty", "owner_lock_released", "processes_stopped")
    public = {key: report.get(key) for key in (*flags, "mode", "actual_model", "openclaw", "image")}
    public["cleanup_error_count"] = len(report.get("cleanup_errors", [None]))
    for key in ("phase", "error_type", "setup_exit_code", "setup_error_codes"):
        if key in report:
            public[key] = report[key]
    fields = ("calls", "completed", "verdict", "cleanup", "source_sha256", "config_sha256", "image")
    turns = [{key: turn.get(key) for key in fields} for turn in report.get("turns", [])]
    public["turns"] = turns
    artifact = report.get("artifact", {})
    public["artifact"] = {key: artifact.get(key) for key in ("wheel_sha256", "versions", "modules")}
    accepted = (
        all(report.get(key) is True for key in flags)
        and mode in {"smoke", "model"}
        and report.get("mode") == mode
        and report.get("actual_model") is (mode == "model")
        and report.get("cleanup_errors") == []
        and len(turns) == 2
        and [turn["verdict"] for turn in turns] == ["invalid", "valid"]
        and all(
            type(turn["calls"]) is int
            and 1 <= turn["calls"] <= (32 if mode == "model" else 1)
            and turn["completed"] is True
            and turn["cleanup"] == "confirmed"
            for turn in turns
        )
        and bool(artifact.get("versions"))
        and bool(artifact.get("modules"))
        and bool(re.fullmatch(r"[0-9a-f]{64}", artifact.get("wheel_sha256", "")))
    )
    if mode == "model":
        provider = report.get("provider", {})
        public["provider"] = {
            key: provider.get(key)
            for key in (
                "requests",
                "forwarded_responses",
                "error_count",
                "endpoint_sha256",
                "requested_model_sha256",
                "response_model_sha256",
                "max_requests",
                "max_completion_tokens",
            )
        }
        hashes = provider.get("response_model_sha256", [])
        accepted = (
            accepted
            and type(provider.get("requests")) is int
            and 4 <= provider["requests"] <= 32
            and provider.get("forwarded_responses") == provider["requests"]
            and provider.get("error_count") == 0
            and provider.get("max_requests") == 32
            and provider.get("max_completion_tokens") == 4096
            and isinstance(hashes, list)
            and 1 <= len(hashes) <= 32
            and all(
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in [
                    provider.get("endpoint_sha256"),
                    provider.get("requested_model_sha256"),
                    *hashes,
                ]
            )
        )
    return public, accepted


def collect(root: Path, mode: str = "smoke") -> tuple[dict[str, Any], bool]:
    """Join sample results to an independent owner-scoped Docker inventory."""
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    public, accepted = observations(report, mode)
    public["report_sha256"] = digest(report_path)
    try:
        owner = (root / "owner/owner").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise ValueError("Invalid owner identity")
        containers = command(
            "docker", "ps", "-aq", "--filter", "label=maf-sandbox.scope=openclaw-bicep-" + owner
        )
        public["independent_owned_container_count"] = len(containers.split())
        public["tool_evidence_sha256"] = digest(root / "tool-evidence.jsonl")
        return public, accepted and not containers
    except Exception as exc:
        public["collection_error_type"] = type(exc).__name__
        return public, False


def main() -> int:
    """Write only bounded Markdown evidence, refusing incomplete collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("smoke", "model"), default="smoke")
    args = parser.parse_args()
    evidence: dict[str, Any] = {"qualified": False}
    try:
        evidence.update(
            {
                key: os.environ.get(key, "")
                for key in (
                    "GITHUB_SHA",
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "RUNNER_OS",
                    "RUNNER_ARCH",
                    "ImageOS",
                    "ImageVersion",
                    "BICEP_IMAGE",
                )
            }
        )
        evidence["platform"] = platform.platform()
        evidence["node"] = command("node", "--version")
        evidence["uv"] = command("uv", "--version")
        evidence["docker"] = json.loads(
            command("docker", "version", "--format", "{{json .Server}}")
        )
        evidence["sample_sha256"] = digest(Path("samples/experimental/openclaw_bicep_e2e/agent.py"))
        root = Path(os.environ["RUNNER_TEMP"]) / "openclaw-bicep-private"
        observed, accepted = collect(root, args.mode)
        evidence.update(observed)
        evidence["worker_python"] = command(str(root / "worker/bin/python"), "--version")
        evidence["qualified"] = accepted
    except Exception as exc:
        evidence["collection_error_type"] = type(exc).__name__
    text = "# OpenClaw native Linux Docker sample\n\n"
    text += (
        f"Requested profile: {args.mode}; actual Gateway, installed worker and Bicep compiler.\n\n"
    )
    text += "Private logs, tokens, sessions and assistant responses are not published.\n\n"
    text += "```json\n" + json.dumps(evidence, indent=2, sort_keys=True) + "\n```\n"
    args.output.write_text(text, encoding="utf-8")
    return 0 if evidence["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
