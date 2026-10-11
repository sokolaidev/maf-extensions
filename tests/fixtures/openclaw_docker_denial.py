"""Control a removal-denial plugin on a dedicated local Docker daemon."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


def outer_docker(host: str, *args: str, timeout: int = 30) -> str:
    if not host.startswith(("npipe:////./pipe/", "unix:///")):
        raise RuntimeError("The parent Docker endpoint must be a local pipe or socket")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
    result = subprocess.run(
        ["docker", "--host", host, *args],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("Dedicated Docker control command failed: " + result.stderr[-2048:])
    return result.stdout.strip()


class DockerRemovalDenial:
    """Arm one exact container target; leave the service's Docker connection reachable."""

    def __init__(self, root: Path, host: str, daemon: str, client: str):
        if (
            re.fullmatch(r"[a-f0-9]{64}", daemon) is None
            or re.fullmatch(r"[a-f0-9]{32}", client) is None
        ):
            raise RuntimeError("Denial requires exact daemon and client identities")
        self.root, self.host, self.daemon, self.client = root, host, daemon, client
        self.nonce = uuid.uuid4().hex
        inspected = json.loads(outer_docker(host, "inspect", daemon))[0]
        if inspected["Config"]["Labels"].get("maf-qualification.authz") != client:
            raise RuntimeError("Denial daemon does not belong to this qualification")
        self.environment = dict(os.environ)
        self.environment["DOCKER_CUSTOM_HEADERS"] = "X-Qualification-Client=" + client

    def arm(self, name: str, container: str) -> None:
        if (
            re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name) is None
            or re.fullmatch(r"[a-f0-9]{64}", container) is None
        ):
            raise RuntimeError("Denial requires an exact compiler identity")
        pending = self.root / "deny-target.json"
        pending.write_text(
            json.dumps({"name": name, "id": container, "nonce": self.nonce}), encoding="utf-8"
        )
        outer_docker(self.host, "cp", str(pending), self.daemon + ":/control/next.json")
        outer_docker(
            self.host, "exec", self.daemon, "mv", "/control/next.json", "/control/deny.json"
        )

    def restore(self) -> None:
        outer_docker(self.host, "exec", self.daemon, "rm", "-f", "/control/deny.json")

    def evidence(self) -> list[dict[str, Any]]:
        destination = self.root / "docker-authz.jsonl"
        outer_docker(self.host, "cp", self.daemon + ":/control/authz.jsonl", str(destination))
        if destination.stat().st_size > 1_048_576:
            raise RuntimeError("Authorization evidence exceeded its bound")
        return [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]


def verify_authorization(
    rows: list[dict[str, Any]],
    removals: list[dict[str, Any]],
    *,
    target: str,
    container: str,
    client: str,
    nonce: str,
    denied: bool,
    after_sequence: int = 0,
) -> int:
    """Join one daemon decision per real command using target, client, fault nonce and order."""
    previous = 0
    for row in rows:
        sequence = row.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= previous:
            raise RuntimeError("Daemon decisions lack unique ordered sequence numbers")
        previous = sequence
    if (
        re.fullmatch(r"[a-f0-9]{32}", client) is None
        or re.fullmatch(r"[a-f0-9]{32}", nonce) is None
    ):
        raise RuntimeError("Daemon decision correlation lacks a client or fault identity")
    matching = [
        r
        for r in rows
        if r.get("client") == client
        and r.get("target") in {target, container}
        and r.get("sequence", 0) > after_sequence
    ]
    if not removals or len(matching) != len(removals):
        raise RuntimeError("Daemon decisions do not match the real removal commands")
    if not all(
        r.get("event") == "docker_authz"
        and r.get("method") == "DELETE"
        and r.get("allowed") is (not denied)
        and r.get("denied") is denied
        and r.get("nonce") == (nonce if denied else "")
        for r in matching
    ):
        raise RuntimeError("Daemon authorization decision does not prove the selected policy")
    return int(matching[-1]["sequence"])
