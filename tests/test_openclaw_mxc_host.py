"""Keep disposable-host preparation explicit and native diagnostics non-qualifying."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
host = importlib.import_module("openclaw_conformance.mxc_host")
smoke = importlib.import_module("openclaw_conformance.mxc_smoke")
Refused = importlib.import_module("openclaw_conformance.evidence").Refused


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(host, "WINDOWS", True)
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Windows",
        "RUNNER_TEMP": str(tmp_path),
        "SYSTEMROOT": str(tmp_path / "Windows"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(host, "identities", lambda _: {"candidate": "pinned"})
    monkeypatch.setattr(host, "file_hash", lambda _: host.PREPARER)
    monkeypatch.setattr(host.shutil, "which", lambda _: "node")
    calls = []

    def capture(args, *positional, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"diagnostic output")

    monkeypatch.setattr(host, "capture", capture)
    return calls


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_OS", "Linux"),
        ("RUNNER_TEMP", ""),
    ],
)
def test_preparation_refused_before_commands_or_output(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
):
    monkeypatch.setenv(key, value)
    output = tmp_path / "output"
    with pytest.raises(Refused):
        host.observe(tmp_path / "installation", output, prepare=True)
    assert runner == []
    assert not output.exists()


@pytest.mark.parametrize("prepare", [False, True])
def test_host_diagnostics_separate_preparation_from_qualification(
    runner, tmp_path: Path, prepare: bool
):
    output = tmp_path / "output"
    assert host.observe(tmp_path / "installation", output, prepare=prepare)
    preparations = [args for args in runner if args[-1] == "prepare-system-drive"]
    assert len(preparations) == int(prepare)
    probes = [i for i, args in enumerate(runner) if args[-1] == "--probe"]
    assert len(probes) == 1 + int(prepare)
    if prepare:
        assert probes[0] < runner.index(preparations[0]) < probes[1]
    report = (output / "host.md").read_text(encoding="utf-8")
    assert "not native execution or confinement proof" in report
    assert "Native smoke is evaluated separately" in report
    assert not list(output.rglob("*.json"))


def test_changed_preparer_refuses_all_commands(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(host, "file_hash", lambda _: "changed")
    output = tmp_path / "output"
    assert not host.observe(tmp_path / "installation", output, prepare=True)
    assert not runner
    assert "host_preparer_identity" in (output / "host.md").read_text()


def test_failed_preparation_does_not_report_success(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = host.capture

    def capture(args, *positional, **kwargs):
        result = original(args, *positional, **kwargs)
        if args[-1] == "prepare-system-drive":
            result.returncode = 1
        return result

    monkeypatch.setattr(host, "capture", capture)
    output = tmp_path / "output"
    assert not host.observe(tmp_path / "installation", output, prepare=True)
    assert runner[-1][-1] == "prepare-system-drive"
    assert "completed: `False`" in (output / "host.md").read_text()


@pytest.mark.parametrize("outside", ["root", "installation"])
def test_preparation_requires_both_paths_beneath_runner_temp(runner, tmp_path: Path, outside: str):
    paths = {"root": tmp_path / "output", "installation": tmp_path / "installation"}
    paths[outside] = tmp_path.parent / "outside"
    with pytest.raises(Refused, match="runner_owned_paths_required"):
        host.require_disposable_runner(**paths)
    assert not runner


def test_output_cannot_mutate_the_installation(runner, tmp_path: Path):
    installation = tmp_path / "installation"
    with pytest.raises(Refused, match="fresh_external_output_required"):
        host.observe(installation, installation / "output", prepare=False)
    assert not runner


def test_debug_events_do_not_complete_missing_native_case(tmp_path: Path):
    logs = tmp_path / "temp/mxc-logs"
    logs.mkdir(parents=True)
    (logs / "native.log").write_text(
        "private unrelated text\nselected isolation tier: appcontainer-dacl\n"
        '{"event":"mxc.ProcessExited","exitCode":0}\n',
        encoding="utf-8",
    )
    campaign = smoke.Campaign(tmp_path, tmp_path / "installation", "none", debug=True)
    try:
        campaign.rows = [("native", False, "fixture absent")]
        assert not campaign.report(None)
        report = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "appcontainer-dacl" in report
        assert "not independent execution proof" in report
        assert "private unrelated text" not in report
        assert "unqualified" in report
    finally:
        campaign.provider.close()


def test_debug_log_extraction_is_bounded(tmp_path: Path):
    logs = tmp_path / "temp/mxc-logs"
    logs.mkdir(parents=True)
    (logs / "native.log").write_bytes(b"x" * 65537 + b'\n{"event":"mxc.ProcessExited"}')
    result = "\n".join(smoke.native_diagnostics(tmp_path))
    assert "exceeded 64 KiB" in result
    assert "ProcessExited" not in result
    assert "SHA-256" not in result


def test_debug_is_explicitly_opt_in(tmp_path: Path):
    provider = smoke.Provider()
    try:
        for enabled in (False, True):
            config = smoke.configuration(
                tmp_path, tmp_path / "plugin", "none", 1234, "token", provider, debug=enabled
            )
            settings = config["plugins"]["entries"]["mxc"]["config"]
            assert settings.get("debug", False) is enabled
    finally:
        provider.close()


def test_hosted_comparison_is_opt_in_and_retains_only_markdown():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text(
            encoding="utf-8"
        )
    )
    job = workflow["jobs"]["openclaw-mxc"]
    assert job["runs-on"] == "windows-latest"
    assert job["strategy"]["matrix"] == {
        "mode": ["none", "ro", "rw"],
        "preparation": ["unprepared", "prepared"],
    }
    for step in job["steps"]:
        assert "github.event_name == 'workflow_dispatch' && inputs.openclaw_mxc" in step["if"]
        assert not step.get("continue-on-error", False)
    artifact = job["steps"][-1]
    assert "always()" in artifact["if"]
    assert all(path.endswith(".md") for path in artifact["with"]["path"].splitlines())


def test_preparation_receives_system_drive_from_windows_root(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("SystemDrive", raising=False)
    original = host.capture

    def capture(args, root, env, *positional, **kwargs):
        assert env["SystemDrive"] == (tmp_path / "Windows").anchor.removesuffix("\\")
        return original(args, root, env, *positional, **kwargs)

    monkeypatch.setattr(host, "capture", capture)
    assert host.observe(tmp_path / "installation", tmp_path / "output", prepare=True)


def test_failed_preparation_retains_bounded_redacted_stderr(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = host.capture
    output = tmp_path / "output"

    def capture(args, root, *positional, **kwargs):
        result = original(args, root, *positional, **kwargs)
        with (root / "setup.log").open("ab") as log:
            if args[-1] == "prepare-system-drive":
                log.write(f"cannot prepare {root}\n".encode() + b"x" * 16384 + b"omitted suffix")
                result.returncode = 1
            else:
                log.write(b"earlier diagnostic\n")
        return result

    monkeypatch.setattr(host, "capture", capture)
    assert not host.observe(tmp_path / "installation", output, prepare=True)
    report = (output / "host.md").read_text(encoding="utf-8")
    last = report.split("## Disposable host preparation", 1)[1]
    assert "cannot prepare <host-diagnostic>" in last
    assert "Stderr exceeded 16 KiB" in last
    assert "earlier diagnostic" not in last
    assert "omitted suffix" not in last
    assert str(output) not in report
    assert "completed: `False`" in report
