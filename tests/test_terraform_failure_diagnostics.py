"""Launcher failures retain bounded evidence without changing consumer verdicts."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from maf_sandbox import ExecResult
from maf_sandbox_terraform._report import format_outcome, report_outcome

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    module = load("diagnostic_runner", ROOT / "images/terraform-sandbox/runner.py")
    metadata = {
        "engine": "terraform",
        "version": "1.16.6",
        "profile": "builtin",
        "binary_sha256": hashlib.sha256(b"engine").hexdigest(),
    }
    (tmp_path / "engine.json").write_text(json.dumps(metadata), encoding="utf-8")
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "INSTALL", tmp_path)
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        if path == Path("/usr/local/bin/terraform"):
            return io.BytesIO(b"engine")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return module, clock


@pytest.mark.parametrize("phase", ["version", "init", "validate", "fmt"])
@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError("private detail"), "timeout"),
        (subprocess.TimeoutExpired("private command", 120), "timeout"),
        (RuntimeError("output limit"), "output_limit"),
        (ValueError("private detail"), "launcher_error"),
    ],
)
def test_failure_identifies_phase_and_time_without_exception_prose(
    launcher, monkeypatch, phase, failure, expected
):
    module, clock = launcher

    def execute(self, command, cwd):
        clock[0] += 2
        if command[1] == phase:
            raise failure
        return {"exit_code": 0, "stdout": '{"terraform_version":"1.16.6"}', "stderr": ""}

    monkeypatch.setattr(module.Supervisor, "execute_phase", execute)
    result = module.execute("terraform", ".", 120)
    diagnostic = result["diagnostics"]
    assert diagnostic == {
        "phase": phase,
        "failure": expected,
        "elapsed_seconds": 2 * (["version", "init", "validate", "fmt"].index(phase) + 1),
        "phase_seconds": 2,
    }
    raw = json.dumps(result).encode()
    assert b"private" not in raw
    assert len(json.dumps(diagnostic)) < 200
    outcome = report_outcome(raw, "terraform")
    assert not outcome.completed and not outcome.valid
    assert outcome.output == "" and "bounded execution" in outcome.reason


def test_success_does_not_add_failure_diagnostics(launcher, monkeypatch):
    module, _ = launcher
    monkeypatch.setattr(
        module.Supervisor,
        "execute_phase",
        lambda *args: {"exit_code": 0, "stdout": '{"terraform_version":"1.16.6"}', "stderr": ""},
    )
    result = module.execute("terraform", ".", 120)
    assert result["error"] is None and "diagnostics" not in result


def test_setup_failure_and_formatting_failure_remain_incomplete(launcher, monkeypatch):
    module, _ = launcher
    setup = module.execute("invalid", ".", 120)
    assert setup["diagnostics"]["phase"] == "setup"

    def execute(self, command, cwd):
        if command[1] == "fmt":
            raise TimeoutError("private detail")
        return {"exit_code": 0, "stdout": '{"terraform_version":"1.16.6"}', "stderr": ""}

    monkeypatch.setattr(module.Supervisor, "execute_phase", execute)
    result = module.execute("terraform", ".", 120, "format")
    assert result["diagnostics"]["phase"] == "fmt"
    assert result["phases"] == {} and "formatted_files" not in result
    assert not format_outcome(json.dumps(result).encode(), "terraform", {}).completed


@pytest.fixture
def live_suite():
    return load("diagnostic_live_suite", ROOT / "tests/test_terraform_avm_offline.py")


@pytest.mark.parametrize("byte", [b"x", b"\xff"])
def test_capture_preserves_result_and_bounds_both_streams(
    live_suite, tmp_path, monkeypatch, capsys, byte
):
    result = ExecResult(stdout_bytes=byte * 20001, stderr_bytes=byte * 4000, exit_code=7)
    record = {}
    command = ["python3", "-I", "/opt/maf-terraform/runner.py", "terraform", ".", "120"]

    async def execute(actual, **kwargs):
        assert actual is command and kwargs == {"working_directory": "/call", "timeout": 125}
        return result

    observed = asyncio.run(
        live_suite.capture_launcher(
            execute, record, command, working_directory="/call", timeout=125
        )
    )
    assert observed is result
    assert record["stdout_bytes"] == 20001 and record["stderr_bytes"] == 4000
    assert len(record["stdout_tail"]) == 8192 and len(record["stderr_tail"]) == 2048
    assert record["exit_code"] == 7 and record["elapsed_seconds"] >= 0
    assert not capsys.readouterr().out and not list(tmp_path.iterdir())
    monkeypatch.setenv("MAF_TERRAFORM_DIAGNOSTICS_DIR", str(tmp_path))
    live_suite.retain_failure("pinned", "docker", record)
    artifact = tmp_path / "pinned-docker.json"
    assert artifact.stat().st_size < 64 * 1024
    assert json.loads(artifact.read_text())["launcher"] == record
    assert "guest output is untrusted" in capsys.readouterr().out


def test_unrelated_commands_do_not_replace_launcher_evidence(live_suite):
    record = {"retained": True}
    result = ExecResult(stdout="refused")

    async def execute(command, **kwargs):
        return result

    assert (
        asyncio.run(live_suite.capture_launcher(execute, record, ["python3", "-c", "probe"]))
        is result
    )
    assert record == {"retained": True}


@pytest.mark.parametrize("failure", [TimeoutError("private detail"), asyncio.CancelledError()])
def test_capture_preserves_exceptions_and_timing(live_suite, failure):
    record = {}

    async def execute(*args, **kwargs):
        raise failure

    with pytest.raises(type(failure)) as observed:
        asyncio.run(
            live_suite.capture_launcher(
                execute, record, ["python3", "-I", "/opt/maf-terraform/runner.py"], timeout=125
            )
        )
    assert observed.value is failure
    assert record["exception_type"] == type(failure).__name__
    assert record["elapsed_seconds"] >= 0 and "private" not in json.dumps(record)


@pytest.mark.parametrize("failure", [None, "verdict", "cleanup"])
def test_live_case_retains_only_failures_after_cleanup(
    live_suite, tmp_path, monkeypatch, capsys, failure
):
    (tmp_path / "receipt.json").write_text("{}")
    monkeypatch.setattr(live_suite, "PREPARED", tmp_path)
    output = tmp_path / "diagnostics"
    monkeypatch.setenv("MAF_TERRAFORM_DIAGNOSTICS_DIR", str(output))
    disposed = []
    raw = ExecResult(stdout='{"diagnostics":{"phase":"init","failure":"timeout"}}')

    async def execute(command, **kwargs):
        return (
            raw
            if command[2:3] == ["/opt/maf-terraform/runner.py"]
            else ExecResult(stdout="refused")
        )

    sandbox = SimpleNamespace(instance_id="test-container", exec=execute)

    async def acquire(*args):
        return sandbox

    async def close():
        pass

    backend = SimpleNamespace(isolation=None, acquire=acquire, aclose=close)

    async def create_backend(name):
        return backend

    async def dispose(*args):
        disposed.append(True)
        if failure == "cleanup":
            raise RuntimeError("cleanup failed")

    async def call(**kwargs):
        await backend.acquire(None, None)
        result = await sandbox.exec(["python3", "-I", "/opt/maf-terraform/runner.py"], timeout=125)
        assert result is raw
        return [SimpleNamespace(text="incomplete" if failure == "verdict" else live_suite.PASSED)]

    monkeypatch.setattr(live_suite, "create_backend", create_backend)
    monkeypatch.setattr(
        live_suite, "SandboxRouter", lambda *a, **kw: SimpleNamespace(dispose_scope=dispose)
    )
    monkeypatch.setattr(
        live_suite, "make_terraform_tools", lambda *a, **kw: [SimpleNamespace(func=call)]
    )
    if failure:
        with pytest.raises(AssertionError if failure == "verdict" else RuntimeError):
            live_suite.test_avm_graph_validates_offline(
                "acas", "pinned", (live_suite.PASSED,), monkeypatch
            )
        retained = json.loads((output / "pinned-acas.json").read_text())
        assert retained["launcher"]["stdout_tail"] == raw.stdout
        assert "failure diagnostics" in capsys.readouterr().out
    else:
        live_suite.test_avm_graph_validates_offline(
            "acas", "pinned", (live_suite.PASSED,), monkeypatch
        )
        assert not output.exists() and not capsys.readouterr().out
    assert disposed == [True]


def test_artifact_write_failure_does_not_hide_original_evidence(
    live_suite, tmp_path, monkeypatch, capsys
):
    blocked = tmp_path / "file"
    blocked.write_text("occupied")
    monkeypatch.setenv("MAF_TERRAFORM_DIAGNOSTICS_DIR", str(blocked))
    live_suite.retain_failure("pinned", "docker", {"exception_type": "TimeoutError"})
    output = capsys.readouterr().out
    assert "TimeoutError" in output and "artifact could not be written" in output


@pytest.mark.workflow
def test_workflow_retains_diagnostics_only_after_avm_failure():
    workflow = yaml.safe_load((ROOT / ".github/workflows/terraform-live.yml").read_text("utf-8"))
    steps = workflow["jobs"]["terraform"]["steps"]
    check = next(step for step in steps if step.get("id") == "avm")
    upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    assert steps.index(upload) > steps.index(check)
    assert upload["if"] == "failure() && steps.avm.outcome == 'failure'"
    assert upload["with"]["path"] == check["env"]["MAF_TERRAFORM_DIAGNOSTICS_DIR"]
    assert not check.get("continue-on-error")
