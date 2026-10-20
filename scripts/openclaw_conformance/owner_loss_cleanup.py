"""Independent cleanup of resources durably registered by the fault supervisor."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .evidence import read, require
from .install import environment


def cleanup(root: Path, case: str) -> dict[str, Any]:
    """Reconcile only fixture-owned resources after every child process tree has stopped."""

    observer_environment = environment(root)

    def docker(*args):
        value = subprocess.run(
            ["docker", *args],
            env=observer_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        require(value.returncode == 0, "independent_observer_unavailable")
        return value.stdout.strip()

    removed = []
    if case == "worker-denied":
        control = root / "daemon-control.json"
        if control.exists():
            value = read(control)
            require(
                re.fullmatch(r"[0-9a-f]{32}", value["client"])
                and value["name"] == "qualification-authz-" + value["client"],
                "daemon_control_scope",
            )
            cleanup_daemon = importlib.import_module("openclaw_docker_denial_check").cleanup_daemon

            cleanup_daemon(root, value["host"], value["name"], value["client"])
        return {"remaining": [], "removed": removed, "scope": "dedicated_daemon", "confirmed": True}
    control = root / "resources.json"
    owner = root / "owner/owner"
    scopes = []
    if owner.exists():
        value = owner.read_text(encoding="ascii")
        require(re.fullmatch(r"[0-9a-f]{32}", value), "invalid_cleanup_owner")
        scopes.append(("openclaw-bicep-" + value, None))
    if control.exists():
        value = read(control)
        require(
            re.fullmatch(r"oc-owner-loss-[0-9a-f]{32}", value["sentinel_name"])
            and re.fullmatch(r"owner-loss-sentinel-[0-9a-f]{32}", value["sentinel_scope"]),
            "invalid_sentinel_control",
        )
        scopes.append((value["sentinel_scope"], value["sentinel_name"]))
    for scope, name in scopes:
        found = docker(
            "ps", "-aq", "--no-trunc", "--filter", "label=maf-sandbox.scope=" + scope
        ).split()
        for identifier in found:
            info = json.loads(docker("inspect", identifier))[0]
            require(
                info["Id"] == identifier
                and info["Config"]["Labels"].get("maf-sandbox.scope") == scope
                and (name is None or info["Name"] == "/" + name),
                "cleanup_scope_changed",
            )
            docker("rm", "-f", identifier)
            removed.append(identifier)
        require(
            not docker("ps", "-aq", "--filter", "label=maf-sandbox.scope=" + scope),
            "cleanup_survivor",
        )
    return {"remaining": [], "removed": removed, "scope": "registered_case", "confirmed": True}
