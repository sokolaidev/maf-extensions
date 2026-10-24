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
        "GITHUB_REPOSITORY": "sokolaidev/maf-extensions",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(name, value)
    bundle = tmp_path / "bundle"
    (bundle / "startup").mkdir(parents=True)
    (bundle / "startup/index.json").write_text("startup", encoding="utf-8")
    (bundle / "helper").write_bytes(b"test helper")
    monkeypatch.setattr(cross.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cross.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cross.cross_compat, "cpu_vendor", lambda: "GenuineIntel")
    cross.cross_compat.record_fixture(bundle, "GenuineIntel")
    peers, roots = {}, {}
    for index, role in enumerate(cross.ROLES, 1):
        root = tmp_path / role
        monkeypatch.setattr(cross, "_boot", lambda index=index: str(index) * 64)
        cross.bootstrap(root, bundle, role, f"100.64.0.{index}")
        peers[role] = json.loads((root / "public" / f"{role}.json").read_text("utf-8"))
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
        (
            "${{ runner.temp }}/mxc-cross/report.json\n${{ runner.temp }}/mxc-cross/service.log\n"
            "${{ runner.temp }}/mxc-cross/*/scratch/*/native.stdout\n"
            "${{ runner.temp }}/mxc-cross/*/scratch/*/native.stderr\n"
        ),
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


@pytest.mark.parametrize("vendor", cross.cross_compat.VENDORS)
def test_matching_producer_and_execution_vendors_are_admitted(participants, vendor):
    _, _, peers = participants
    for role, peer in peers.items():
        peer["profile"]["cpu_vendor"] = vendor
        peer["cpu_vendor"] = vendor if role != "coordinator" else None
    cross.validate_peers(peers, cross._binding())


@pytest.mark.parametrize("role", ["a", "b"])
@pytest.mark.parametrize("vendor", ["AuthenticAMD", None, "", "unknown"])
def test_execution_cpu_must_match_fixture_even_when_workers_match(participants, role, vendor):
    _, _, peers = participants
    peers[role]["cpu_vendor"] = vendor
    if role == "a":
        peers["b"]["cpu_vendor"] = vendor
    with pytest.raises(RuntimeError, match="execution CPU vendor differs"):
        cross.validate_peers(peers, cross._binding())


@pytest.mark.parametrize("change", ["missing", "unknown", "null", "different"])
def test_discovery_refuses_legacy_or_changed_fixture_provenance(participants, change):
    _, _, peers = participants
    for peer in peers.values():
        if change == "missing":
            del peer["profile"]["cpu_vendor"]
        else:
            peer["profile"]["cpu_vendor"] = {
                "unknown": "unknown",
                "null": None,
                "different": "AuthenticAMD",
            }[change]
    with pytest.raises(RuntimeError):
        cross.validate_peers(peers, cross._binding())


@pytest.mark.parametrize("role", ["a", "b"])
def test_execution_rechecks_local_cpu_before_opening_a_worker(participants, monkeypatch, role):
    roots, bundle, peers = participants
    monkeypatch.setattr(cross.cross_compat, "cpu_vendor", lambda: "AuthenticAMD")
    with pytest.raises(RuntimeError, match="local execution CPU vendor differs"):
        cross.Worker(roots[role], bundle, role, peers)
    assert not (roots[role] / "session").exists()


def test_coordinator_does_not_probe_its_cpu(participants, monkeypatch, tmp_path):
    _, bundle, peers = participants
    monkeypatch.setattr(
        cross.cross_compat, "cpu_vendor", lambda: pytest.fail("coordinator probed CPU")
    )
    root = tmp_path / "other-coordinator"
    cross.bootstrap(root, bundle, "coordinator", "100.64.0.3")
    cross.validate_local(bundle, "coordinator", peers["coordinator"])
    assert json.loads((root / "public/coordinator.json").read_text("utf-8"))["cpu_vendor"] is None


@pytest.mark.parametrize("change", ["missing", "cpu", "helper", "startup", "attempt", "source"])
def test_fixture_provenance_is_bound_to_content_and_attempt(participants, change):
    _, bundle, _ = participants
    path = bundle / "fixture.json"
    fixture = json.loads(path.read_text("utf-8"))
    if change == "missing":
        path.unlink()
    elif change == "cpu":
        del fixture["profile"]["cpu_vendor"]
        path.write_text(json.dumps(fixture), encoding="utf-8")
    elif change in {"helper", "startup"}:
        target = bundle / ("helper" if change == "helper" else "startup/index.json")
        target.write_bytes(b"changed")
    else:
        fixture["binding"]["GITHUB_RUN_ATTEMPT" if change == "attempt" else "GITHUB_SHA"] = "other"
        path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(RuntimeError):
        cross.cross_compat.load_fixture(bundle)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "processor: 0\nvendor_id: GenuineIntel\n\nprocessor: 1\nvendor_id: GenuineIntel\n",
            "GenuineIntel",
        ),
        ("processor: 0\nvendor_id: AuthenticAMD\n", "AuthenticAMD"),
        ("", None),
        ("processor: 0\n", None),
        ("processor: 0\nvendor_id: GenuineIntel\n\nprocessor: 1\n", None),
        ("processor: 0\nvendor_id: GenuineIntel\n\nprocessor: 1\nvendor_id: AuthenticAMD\n", None),
        ("vendor_id: unknown\n", None),
        ("vendor_id: GenuineIntel\nvendor_id: GenuineIntel\n", None),
    ],
)
def test_cpu_probe_requires_recognized_unambiguous_vendor(monkeypatch, content, expected):
    monkeypatch.setattr(cross.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cross.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: content)
    if expected is None:
        with pytest.raises(RuntimeError, match="CPU vendor"):
            cross.cross_compat.cpu_vendor()
    else:
        assert cross.cross_compat.cpu_vendor() == expected


def test_cpu_probe_refuses_unavailable_proc_data(monkeypatch):
    monkeypatch.setattr(cross.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cross.platform, "machine", lambda: "x86_64")

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "read_text", unavailable)
    with pytest.raises(RuntimeError, match="CPU vendor is unavailable"):
        cross.cross_compat.cpu_vendor()


@pytest.mark.parametrize("mode", ["discover", "node"])
def test_cli_refuses_incompatible_hosts_before_execution_and_reports_failure(
    participants, monkeypatch, mode
):
    roots, bundle, peers = participants
    peers["b"]["cpu_vendor"] = "AuthenticAMD"
    root = roots["a"]
    (root / "peers.json").write_text(json.dumps(peers), encoding="utf-8")
    for role in ("b", "coordinator"):
        destination = root / "discovery" / role
        destination.mkdir(parents=True)
        (destination / f"{role}.json").write_text(json.dumps(peers[role]), encoding="utf-8")
    monkeypatch.setattr(
        cross.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    monkeypatch.setattr(
        cross, "serve", lambda *args: pytest.fail("incompatible host served requests")
    )
    monkeypatch.setattr(
        cross, "orchestrate", lambda *args: pytest.fail("incompatible run acquired ownership")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cross_runner", mode, "--root", str(root), "--bundle", str(bundle), "--role", "a"],
    )
    with pytest.raises(RuntimeError, match="execution CPU vendor differs"):
        cross.main()
    report = json.loads((root / "report.json").read_text("utf-8"))
    assert report["status"] == "failed"
    assert report["phase"] in {"discover", "admission"}
    assert "execution CPU vendor differs" in report["error"]
    assert not (root / "session").exists()


def test_fixture_build_records_producer_after_native_controls(participants, monkeypatch, tmp_path):
    import tarfile

    fixture = importlib.import_module("scripts.experiments.mxc_session_patch.cross_fixture")
    root = tmp_path / "build"
    monkeypatch.setattr(fixture.native, "hypervisor", lambda: "KVM")
    monkeypatch.setattr(fixture.native, "sources", lambda *args: [])
    monkeypatch.setattr(fixture.native, "build", lambda *args: None)
    monkeypatch.setattr(fixture.native, "helper", lambda root: root / "helper")
    stages = []

    def run(root, stage, *args):
        stages.append(stage)
        if stage == "prepare-agent":
            (root / "images/agent").mkdir(parents=True)
        if stage == "native-state":
            assert not (root / "bundle/fixture.json").exists()
            (root / "helper").write_bytes(b"helper")
            startup = root / "images/agent/snapshot"
            startup.mkdir()
            (startup / "index.json").write_text("startup", encoding="utf-8")

    monkeypatch.setattr(fixture.native, "run", run)
    fixture.build(root)
    assert stages[-1] == "native-state"
    profile = cross.cross_compat.load_fixture(root / "bundle")
    assert profile["cpu_vendor"] == "GenuineIntel"
    with tarfile.open(root / "bundle.tar.gz") as archive:
        assert "bundle/fixture.json" in archive.getnames()


@pytest.mark.parametrize(("system", "machine"), [("Windows", "AMD64"), ("Linux", "aarch64")])
def test_cpu_probe_refuses_unsupported_platform(monkeypatch, system, machine):
    monkeypatch.setattr(cross.platform, "system", lambda: system)
    monkeypatch.setattr(cross.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError, match="requires Linux x86_64"):
        cross.cross_compat.cpu_vendor()


def test_fixture_producer_vendor_cannot_change_during_build(participants, monkeypatch):
    _, bundle, _ = participants
    before = (bundle / "fixture.json").read_bytes()
    monkeypatch.setattr(cross.cross_compat, "cpu_vendor", lambda: "AuthenticAMD")
    with pytest.raises(RuntimeError, match="producer CPU vendor changed"):
        cross.cross_compat.record_fixture(bundle, "GenuineIntel")
    assert (bundle / "fixture.json").read_bytes() == before
