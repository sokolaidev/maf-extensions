"""Linux replacement-startup faults and independent dedicated-daemon observations."""

from __future__ import annotations

import importlib
import re
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .evidence import file_hash, read, require, write
from .install import FIXTURES
from .owner_loss_evidence import DENIAL_SOURCE, one

DENIAL_REFERENCE = (
    "docker:29.8.2-dind@sha256:6316d612722fd7fb2bd7edf9fcbb4439d08915cfb2e0e0b28bf16675fc9f3314"
)
DENIAL_IMAGE = "sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1"


def connection_probe(environment: dict[str, str]) -> int:
    """Probe daemon reachability through a server-side container inventory."""
    return subprocess.run(
        ["docker", "ps", "-aq"], env=environment, capture_output=True, timeout=15
    ).returncode


def denied_case(root: Path, prepared: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    """Run the real removal policy in a disposable daemon without changing the parent."""
    from .owner_loss_cases import case

    require(
        file_hash(FIXTURES / "openclaw_docker_authz/main.go") == DENIAL_SOURCE,
        "denial_source_changed",
    )
    host = importlib.import_module("openclaw_docker_denial_check")
    control_type = importlib.import_module("openclaw_docker_denial").DockerRemovalDenial
    observed: dict[str, Any] = {"case": "worker-denied", "outcome": "fail"}

    def qualify(settings):
        nonlocal observed
        control = control_type(
            root, settings.authz_outer_host, settings.authz_daemon, settings.authz_client
        )
        observed = case(root / "gateway", prepared, candidate, "worker-denied", control)
        return observed

    try:
        host.run(
            SimpleNamespace(
                root=root,
                image=candidate["image"],
                daemon_image=DENIAL_REFERENCE,
                classic_store=True,
                config=root / "unused",
                bicep_config=prepared / "bicepconfig.json",
                openclaw=prepared / "openclaw/node_modules/openclaw",
            ),
            qualifier=qualify,
        )
        report = read(root / "docker-denial-host-report.json")
        observed["denial"] = {
            **read(root / "gateway/denial-decisions.json"),
            "daemon_removed": report["dedicated_daemon_removed"],
            "parent_unchanged": report["parent_daemon_unchanged"],
            "image": report["daemon_image"],
        }
        observed["daemon"] = {
            "registration_sha256": file_hash(root / "daemon-resources.json"),
            "control_sha256": file_hash(root / "daemon-control.json"),
            **{
                key: report[key]
                for key in (
                    "daemon_image",
                    "daemon_version",
                    "daemon_storage_driver",
                    "daemon_tls",
                    "daemon_unchanged",
                    "plugin_source_sha256",
                    "plugin_binary_sha256",
                    "go_version",
                )
            },
        }
    except (Exception, KeyboardInterrupt) as exc:
        observed.update(outcome="fail", failure=type(exc).__name__)
        if isinstance(exc, RuntimeError):
            reasons = {
                "Dedicated daemon certificates not ready": "daemon_certificates",
                "Dedicated daemon did not become ready": "daemon_readiness",
                "Expected a distinct authorization-enabled daemon": "daemon_authorization",
                "Dedicated daemon loaded a different prepared image": "daemon_compiler_identity",
                "Dedicated daemon retained fixture containers": "daemon_orphans",
                "Dedicated daemon volume survived cleanup": "daemon_volume_cleanup",
                "Dedicated daemon survived cleanup": "daemon_cleanup",
            }
            observed["failure"] = reasons.get(str(exc), "RuntimeError")
    finally:
        if sys.platform == "linux":
            signal.alarm(0)
        write(root / "result.json", observed)
    return observed


def inventory(root: Path, *, remove: bool) -> dict[str, Any]:
    """Observe retained daemon/volume identities; collection never removes a survivor."""
    host = importlib.import_module("openclaw_docker_denial_check")
    control = read(root / "daemon-control.json")
    client, name = control["client"], control["name"]
    require(
        re.fullmatch(r"[0-9a-f]{32}", client) and name == "qualification-authz-" + client,
        "daemon_control_scope",
    )
    endpoint = control["host"]
    registered = read(root / "daemon-resources.json")
    require(
        registered["client"] == client
        and re.fullmatch(r"[0-9a-f]{64}", registered["daemon"])
        and isinstance(registered["volumes"], list)
        and registered["volumes"]
        and all(
            isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", v)
            for v in registered["volumes"]
        ),
        "daemon_registration",
    )
    require(
        host.outer_docker(endpoint, "info", "--format", "{{.ID}}") == registered["parent"],
        "parent_daemon_changed",
    )

    def survivors():
        containers = host.outer_docker(
            endpoint,
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=maf-qualification.authz=" + client,
        ).split()
        exact = host.outer_docker(
            endpoint, "ps", "-aq", "--no-trunc", "--filter", "id=" + registered["daemon"]
        ).split()
        volumes = host.outer_docker(endpoint, "volume", "ls", "--format", "{{.Name}}").splitlines()
        return sorted(set(containers + exact) | (set(registered["volumes"]) & set(volumes)))

    remaining = survivors()
    removed = []
    if remove and remaining:
        host.cleanup_daemon(root, endpoint, name, client)
        removed = remaining
        remaining = survivors()
    return {
        "remaining": remaining,
        "removed": removed,
        "scope": "dedicated_daemon",
        "confirmed": not remaining,
    }


def verify_failure(value: dict[str, Any], name: str) -> None:
    """Require causal fault controls, failed startup, restoration and product recovery."""
    rows = value["events"]
    refused, ready = one(rows, "recovery_refused"), one(rows, "replacement_ready")
    blocked, restored = one(rows, "docker_fault_blocked"), one(rows, "docker_fault_restored")
    fault = one(rows, "actor_killed")
    require(
        fault["mono_ns"]
        < blocked["mono_ns"]
        < refused["mono_ns"]
        < restored["mono_ns"]
        < ready["ready_ns"]
        and blocked["kind"] == restored["kind"] == name
        and blocked["resources"] == refused["resources"] == restored["resources"]
        and len(blocked["resources"]) == 1
        and blocked["sentinel_survived"] is True
        and restored["sentinel_survived"] is True
        and blocked["engine"] == restored["engine"],
        "docker_fault_controls",
    )
    require(
        not any(
            r["event"] == "startup_ready"
            and blocked["mono_ns"] < r["mono_ns"] < restored["mono_ns"]
            for r in value["transport"]
        ),
        "blocked_startup_ready",
    )
    if name == "worker-unavailable":
        require(
            blocked["context_private"] is True
            and blocked["endpoint_reserved"] is True
            and type(blocked["probe_returncode"]) is int
            and blocked["probe_returncode"] != 0
            and restored["probe_returncode"] == 0
            and restored["context_same"] is True,
            "connection_fault_controls",
        )
    else:
        daemon = value["daemon"]
        require(
            daemon["daemon_image"] == DENIAL_IMAGE
            and daemon["daemon_version"] == "29.8.2"
            and daemon["daemon_storage_driver"] == "overlay2"
            and daemon["daemon_tls"] is True
            and daemon["daemon_unchanged"] is True
            and daemon["plugin_source_sha256"] == DENIAL_SOURCE
            and re.fullmatch(r"[0-9a-f]{64}", daemon["plugin_binary_sha256"])
            and daemon["go_version"] == "go version go1.27.3 linux/amd64",
            "denial_daemon_identity",
        )
        require(
            all(
                re.fullmatch(r"[0-9a-f]{64}", daemon[key])
                for key in ("registration_sha256", "control_sha256")
            ),
            "daemon_registration_digest",
        )
        denied = value["denial"]
        require(
            denied["removals"]
            == [
                r
                for r in value["transport"]
                if r["event"] == "docker_removal" and r["returncode"] != 0
            ]
            and all(
                blocked["mono_ns"] < r["mono_ns"] < refused["mono_ns"] for r in denied["removals"]
            ),
            "denial_removal_binding",
        )
        authorize = importlib.import_module("openclaw_docker_denial").verify_authorization
        sequence = authorize(
            denied["decisions"],
            denied["removals"],
            target=denied["target"],
            container=denied["container"],
            client=denied["client"],
            nonce=denied["nonce"],
            denied=True,
        )
        removals = denied["allowed_removals"]
        require(
            removals
            == [
                r
                for r in value["transport"]
                if r["event"] == "docker_removal"
                and r["returncode"] == 0
                and r["container"] == denied["container"]
            ]
            and all(restored["mono_ns"] < r["mono_ns"] < ready["ready_ns"] for r in removals),
            "restored_removal_binding",
        )
        authorize(
            denied["restored_decisions"],
            removals,
            target=denied["target"],
            container=denied["container"],
            client=denied["client"],
            nonce=denied["nonce"],
            denied=False,
            after_sequence=sequence,
        )
