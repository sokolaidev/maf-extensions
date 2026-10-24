"""Cross-runner orchestration controls without a native hypervisor or external overlay."""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from queue import Queue

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    cross = importlib.import_module("scripts.experiments.mxc_session_patch.cross_runner")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def participants(tmp_path, monkeypatch):
    for name, value in {
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(name, value)
    bundle = tmp_path / "bundle"
    (bundle / "startup").mkdir(parents=True)
    (bundle / "startup/index.json").write_text("startup", encoding="utf-8")
    (bundle / "helper").write_bytes(b"test helper")
    peers, roots = {}, {}
    for index, role in enumerate(cross.ROLES, 1):
        root = tmp_path / role
        monkeypatch.setattr(cross, "_boot", lambda index=index: str(index) * 64)
        cross.bootstrap(root, bundle, role, f"100.64.0.{index}")
        peers[role] = json.loads((root / "public" / f"{role}.json").read_text("utf-8"))
        peers[role]["profile"]["platform"] = "Linux"
        roots[role] = root
    for root in roots.values():
        for role, peer in peers.items():
            (root / f"{role}.pem").write_text(peer["certificate"], encoding="ascii")
        (root / "trust.pem").write_text(
            "".join(p["certificate"] for p in peers.values()), encoding="ascii"
        )
    return roots, bundle, peers


def test_bootstrap_exposes_no_private_key(participants):
    roots, _, peers = participants
    cross.validate_peers(peers, cross._binding())
    for role, root in roots.items():
        public = list((root / "public").iterdir())
        assert [p.name for p in public] == [f"{role}.json"]
        assert b"PRIVATE KEY" not in public[0].read_bytes()
        assert b"PRIVATE KEY" in (root / "key.pem").read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "commit",
        "role",
        "address",
        "same-machine",
        "same-cert",
        "profile",
        "certificate-name",
        "extra",
    ],
)
def test_discovery_refuses_mixed_or_ambiguous_participants(participants, change):
    _, _, peers = participants
    peers = copy.deepcopy(peers)
    target = peers["b"]
    if change == "attempt":
        target["binding"]["GITHUB_RUN_ATTEMPT"] = "2"
    elif change == "commit":
        target["binding"]["GITHUB_SHA"] = "b" * 40
    elif change == "role":
        target["role"] = "a"
    elif change == "address":
        target["address"] = "127.0.0.1"
    elif change == "same-machine":
        target["boot"] = peers["a"]["boot"]
    elif change == "same-cert":
        target["certificate"] = peers["a"]["certificate"]
    elif change == "profile":
        target["profile"]["helper"] = "different"
    elif change == "certificate-name":
        peers["a"]["certificate"], target["certificate"] = (
            target["certificate"],
            peers["a"]["certificate"],
        )
    else:
        target["private_key"] = "unexpected"
    with pytest.raises(RuntimeError):
        cross.validate_peers(peers, cross._binding())


@contextmanager
def endpoint(factory, root, address=("127.0.0.1", 0)):
    ready = Queue()
    errors = []

    def work():
        service = factory()
        context = cross.api.server_context(
            root / "certificate.pem", root / "key.pem", root / "trust.pem"
        )
        try:
            with cross.api.Server(address, context, service) as server:
                server.timeout = 0.1
                ready.put((service, server.server_address))
                while not service.done:
                    server.handle_request()
        except BaseException as error:
            errors.append(error)
        finally:
            if isinstance(service, cross.Service):
                service.db.close()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    service, actual = ready.get(timeout=10)
    try:
        yield service, actual
    finally:
        service.done = True
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert not errors


def test_three_endpoints_restore_replay_fence_and_reconcile(participants, monkeypatch):
    roots, bundle, peers = participants
    addresses = {}
    original_client = cross._client

    def client(root, peers, role):
        result = original_client(root, peers, role)
        result.address = addresses[role]
        return result

    monkeypatch.setattr(cross, "_client", client)
    monkeypatch.setattr(cross.api, "MAX_FRAME", 100_000)
    executions = []

    def execute(
        helper, startup, work, code, output_limit, *, before_spawn, before_start, checkpoint_limits
    ):
        before_spawn()
        with subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        ) as child:
            try:
                before_start(child)
                if code == cross.SEED:
                    assert (startup / "index.json").read_text("utf-8") == "startup"
                    state = 41
                else:
                    assert code == cross.NEXT
                    assert (startup / "index.json").read_text("utf-8") == "41"
                    state = 42
                candidate = work / "candidate"
                candidate.mkdir()
                (candidate / "index.json").write_text(str(state), encoding="utf-8")
                (candidate / "memory.bin").write_bytes(b"x" * 128_000)
                executions.append((work, state))
            finally:
                child.kill()
                child.wait(timeout=10)
        return json.dumps({"console_base64": cross.api._encoded(str(state).encode())}).encode()

    monkeypatch.setattr(cross.remote_host, "execute", execute)
    service_context = endpoint(
        lambda: cross.Service(roots["coordinator"], peers), roots["coordinator"]
    )
    _, addresses["coordinator"] = service_context.__enter__()
    try:
        with (
            endpoint(lambda: cross.Worker(roots["a"], bundle, "a", peers), roots["a"]) as (
                a,
                addresses["a"],
            ),
            endpoint(lambda: cross.Worker(roots["b"], bundle, "b", peers), roots["b"]) as (
                b,
                addresses["b"],
            ),
        ):

            def restart():
                nonlocal service_context
                service_context.__exit__(None, None, None)
                service_context = endpoint(
                    lambda: cross.Service(roots["coordinator"], peers),
                    roots["coordinator"],
                    addresses["coordinator"],
                )
                service_context.__enter__()

            transports = {r: client(roots["coordinator"], peers, r) for r in cross.ROLES}
            report = cross.scenario(
                transports["a"], transports["b"], transports["coordinator"], restart
            )
            assert len(report["controls"]) == 8
            assert report["seed_checkpoint_bytes"] == 128_002
            assert report["replacement_checkpoint_bytes"] == 128_002
            assert [state for _, state in executions] == [41, 42]
            assert executions[0][0].is_relative_to(roots["a"])
            assert executions[1][0].is_relative_to(roots["b"])
            assert not list((roots["a"] / "session/scratch").iterdir())
            assert not list((roots["b"] / "session/scratch").iterdir())
            # A worker certificate grants publication access, never test orchestration.
            with pytest.raises(cross.api.Refused, match="not authorized"):
                cross.call(client(roots["a"], peers, "b"), "ready")
            with pytest.raises(cross.api.Refused, match="not authorized"):
                cross.call(client(roots["a"], peers, "coordinator"), "stop")
            for r in ("a", "b"):
                cross.call(transports[r], "finish", passed=True)
            assert a.passed and b.passed
    finally:
        service_context.__exit__(None, None, None)


@pytest.mark.workflow
def test_workflow_requires_oidc_and_overlapping_roles():
    path = Path(__file__).parents[1] / ".github/workflows/mxc-cross-runner.yml"
    workflow = yaml.safe_load(path.read_text("utf-8-sig"))
    assert set(workflow[True]) == {"workflow_dispatch"}
    participants = workflow["jobs"]["participants"]
    assert participants["needs"] == "fixture"
    assert participants["strategy"]["max-parallel"] == 3
    assert set(participants["strategy"]["matrix"]["role"]) == set(cross.ROLES)
    assert participants["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
    }
    tailscale = next(s for s in participants["steps"] if s.get("uses", "").startswith("tailscale/"))
    assert tailscale["with"]["audience"] == "${{ secrets.MXC_TAILSCALE_AUDIENCE }}"
    assert "oauth-secret" not in tailscale["with"]
    assert tailscale["with"]["args"] == "--accept-dns=false"
    steps = participants["steps"]
    route_guard = steps[steps.index(tailscale) + 1]
    assert route_guard["name"] == "Disable tailnet DNS and subnet routes before discovery"
    assert "if" not in route_guard and "continue-on-error" not in route_guard
    assert steps[steps.index(route_guard) + 1]["name"] == (
        "Create runner-local credentials and public discovery"
    )
    assert participants["steps"][-1]["with"]["if-no-files-found"] == "error"
    uploads = [
        s["with"]["path"]
        for s in participants["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert uploads == [
        "${{ runner.temp }}/mxc-cross/public/${{ matrix.role }}.json",
        "${{ runner.temp }}/mxc-cross/report.json\n${{ runner.temp }}/mxc-cross/service.log\n",
    ]


@pytest.mark.workflow
@pytest.mark.skipif(sys.platform != "linux", reason="The participant jobs use Linux Bash")
@pytest.mark.parametrize(
    ("set_status", "route_all", "corp_dns", "succeeds"),
    [
        (0, False, False, True),
        (7, False, False, False),
        (0, True, False, False),
        (0, False, True, False),
    ],
)
def test_route_guard_refuses_failed_or_unapplied_preferences(
    tmp_path, set_status, route_all, corp_dns, succeeds
):
    path = Path(__file__).parents[1] / ".github/workflows/mxc-cross-runner.yml"
    steps = yaml.safe_load(path.read_text("utf-8-sig"))["jobs"]["participants"]["steps"]
    guard = next(
        s
        for s in steps
        if s.get("name") == "Disable tailnet DNS and subnet routes before discovery"
    )
    stub = r"""
sudo() {
  case "$*" in
    "tailscale set --accept-dns=false --accept-routes=false") return "$SET_STATUS" ;;
    "tailscale debug prefs") printf '%s\n' "$PREFS" ;;
    *) return 99 ;;
  esac
}
"""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", stub + guard["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "SET_STATUS": str(set_status),
            "PREFS": json.dumps({"RouteAll": route_all, "CorpDNS": corp_dns}),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is succeeds, result.stderr
    assert not result.stdout
