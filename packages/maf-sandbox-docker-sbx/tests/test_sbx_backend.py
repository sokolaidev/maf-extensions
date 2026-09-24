"""The backend against a scripted ``sbx``: command lines, ownership, refusals and disposal."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    Isolation,
    OsFamily,
    SandboxBackend,
    SandboxKey,
    SandboxSpec,
)

from maf_sandbox_docker_sbx import (
    SbxDaemonFault,
    SbxError,
    SbxHostNotConfined,
    SbxLoginRequired,
    SbxSandboxBackend,
    SbxSandboxConfig,
)
from maf_sandbox_docker_sbx._backend import (
    _EXEC_SCRIPT,
    _KILL_SCRIPT,
    _MARKER,
    _MOUNT_SCRIPT,
    _Result,
    sandbox_name,
)

KEY = SandboxKey(scope="tenant", thread_id="thread", agent_id="agent")
GUEST_MOUNT = "/host/ws"


def _spec(**overrides: object) -> SandboxSpec:
    return SandboxSpec(kind="kind", **overrides)  # type: ignore[arg-type]


def _ok(stdout: bytes = b"", stderr: bytes = b"") -> _Result:
    return _Result(0, stdout, stderr)


def _decode(argument: str) -> str:
    return base64.b64decode(argument[1:]).decode()


class FakeSbx:
    """Answers ``sbx`` the way v0.45.1 did, keeping a listing and each workspace's host path."""

    def __init__(self, backend: SbxSandboxBackend) -> None:
        self.backend = backend
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float | None] = []
        self.sandboxes: dict[str, str] = {}
        self.forwarding = b"false\n"
        self.servers: list[object] = []
        self.exec_hook: Callable[[tuple[str, ...]], _Result | None] = lambda _args: None
        self.rm_result: _Result | None = None
        self.mount_exit = 0
        self.unmounted_once = False
        backend._sbx = self  # type: ignore[method-assign]

    async def __call__(self, *args: str, timeout: float | None = None) -> _Result:
        self.calls.append(args)
        self.timeouts.append(timeout)
        match args:
            case ("settings", "get", "ssh.agentForwardingEnabled"):
                return _ok(self.forwarding)
            case ("mcp", "ls", "--json"):
                return _ok(json.dumps({"servers": self.servers}).encode())
            case ("ls", "--json"):
                rows = [
                    {"name": name, "id": f"id-{name}", "workspaces": [workspace]}
                    for name, workspace in self.sandboxes.items()
                ]
                return _ok(json.dumps({"sandboxes": rows}).encode())
            case ("create", "shell", "--name", name, *_rest):
                self.sandboxes[name] = args[-1]
                return _ok()
            case ("rm", "--force", name):
                if self.rm_result is not None:
                    return self.rm_result
                if self.sandboxes.pop(name, None) is None:
                    return _Result(1, b"", f"error: sandbox '{name}' not found\n".encode())
                return _ok()
            case ("exec", _name, "pwd"):
                return _ok(f"{GUEST_MOUNT}\n".encode())
            case ("exec", "-u", "root", _name, "sh", "-c", script, *_rest) if (
                script == _MOUNT_SCRIPT
            ):
                return _Result(self.mount_exit, b"", b"")
            case ("exec", name, "sh", "-c", script, "maf-sbx", nonce, _pid, _marker, *encoded) if (
                script == _EXEC_SCRIPT
            ):
                hooked = self.exec_hook(args)
                if hooked is not None:
                    return hooked
                if self.unmounted_once:
                    self.unmounted_once = False
                    return _Result(1, b"", f"{nonce}-unmounted\n".encode())
                argv = [_decode(item) for item in encoded[1:]]
                if argv[0] == "cat":
                    host = self._host(name, argv[1])
                    return _Result(0, host.read_bytes(), f"{nonce}\n".encode())
                return _Result(0, b"ran", f"{nonce}\n".encode())
            case ("exec", _name, "sh", "-c", script, "maf-sbx", _pid) if script == _KILL_SCRIPT:
                return _ok()
            case _:
                raise AssertionError(f"unexpected sbx call {args}")

    def _host(self, name: str, guest: str) -> Path:
        workspace = Path(self.sandboxes[name])
        return workspace / guest.split("/")[-1]


@pytest.fixture
def backend(tmp_path: Path) -> SbxSandboxBackend:
    return SbxSandboxBackend(SbxSandboxConfig(workspace_root=tmp_path / "root"))


@pytest.fixture
def sbx(backend: SbxSandboxBackend) -> FakeSbx:
    return FakeSbx(backend)


class TestDeclarations:
    def test_microvm_closed_posix_and_the_workspace_capabilities(self, backend):
        declared = backend.declarations
        assert backend.isolation is Isolation.MICROVM
        assert isinstance(backend, SandboxBackend)
        assert declared.egress_modes == frozenset({Egress.CLOSED})
        assert declared.os_families == frozenset({OsFamily.POSIX})
        assert declared.observes_egress is False
        assert declared.capabilities == frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
                Capability.FILES_DELETE,
                Capability.RECLAIM,
            }
        )

    @pytest.mark.parametrize(
        "overrides",
        [{"name_prefix": "Bad"}, {"name_prefix": ""}, {"cpus": 0}, {"memory": "lots"}],
    )
    def test_config_refuses_values_sbx_would_reject(self, overrides):
        with pytest.raises(ValueError):
            SbxSandboxConfig(**overrides)  # type: ignore[arg-type]


class TestNames:
    def test_a_name_sbx_accepts_that_carries_the_whole_key_and_kind(self):
        name = sandbox_name("maf", KEY, "kind")
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name) and len(name) <= 63
        assert sandbox_name("maf", KEY, "other") != name
        call = SandboxKey(scope="tenant", thread_id="thread", agent_id="agent", call_id="c")
        assert sandbox_name("maf", call, "kind") != name
        agent = SandboxKey(scope="tenant", thread_id="thread", agent_id="agent-2")
        # One conversation shares a prefix, so a scope purge finds every agent's sandbox.
        assert sandbox_name("maf", agent, "kind")[:17] == name[:17]


class TestHostChecks:
    def test_ssh_agent_forwarding_is_refused_before_anything_is_created(self, backend, sbx):
        sbx.forwarding = b"true\n"
        with pytest.raises(SbxHostNotConfined, match="ssh.agentForwardingEnabled false"):
            asyncio.run(backend.acquire(KEY, _spec()))
        assert not any(call[0] == "create" for call in sbx.calls)

    def test_a_registered_mcp_server_is_refused(self, backend, sbx):
        sbx.servers = [{"name": "github"}]
        with pytest.raises(SbxHostNotConfined, match="sbx mcp rm"):
            asyncio.run(backend.acquire(KEY, _spec()))

    def test_a_lapsed_login_names_sbx_login(self, backend, sbx):
        async def lapsed(*args: str, timeout: float | None = None) -> _Result:
            return _Result(1, b"", b"error: not logged in; run sbx login\n")

        backend._sbx = lapsed  # type: ignore[method-assign]
        with pytest.raises(SbxLoginRequired, match="Run `sbx login`"):
            asyncio.run(backend.check_host())


class TestAcquire:
    def test_a_cold_acquire_creates_closed_bounded_and_mounted(self, backend, sbx, tmp_path):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        name = sandbox_name("maf", KEY, "kind")
        create = next(call for call in sbx.calls if call[0] == "create")
        workspace = tmp_path / "root" / name / "ws"
        assert create == (
            "create",
            "shell",
            "--name",
            name,
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--skills",
            "off",
            "--deny-network",
            "**",
            "--quiet",
            str(workspace),
        )
        mount = next(call for call in sbx.calls if _MOUNT_SCRIPT in call)
        assert mount[-3:] == ("/maf-sandbox", GUEST_MOUNT, "create")
        assert sandbox.instance_id == f"id-{name}"
        assert (workspace / _MARKER).exists() and (workspace / "work").is_dir()
        meta = json.loads((tmp_path / "root" / name / "meta.json").read_text())
        assert meta["work_dir"] == "/maf-sandbox/work" and meta["guest_mount"] == GUEST_MOUNT

    def test_an_image_is_the_template(self, backend, sbx):
        asyncio.run(backend.acquire(KEY, _spec(image="example/image:1")))
        create = next(call for call in sbx.calls if call[0] == "create")
        assert create[create.index("--template") + 1] == "example/image:1"

    def test_a_parent_the_image_already_has_is_refused_and_the_sandbox_removed(self, backend, sbx):
        sbx.mount_exit = 3
        with pytest.raises(ValueError, match="already exists in the image"):
            asyncio.run(backend.acquire(KEY, _spec()))
        assert sbx.sandboxes == {}

    def test_a_base_directly_under_the_root_is_refused(self, backend, sbx):
        with pytest.raises(ValueError, match="parent"):
            asyncio.run(backend.acquire(KEY, _spec(work_dir="/work")))

    def test_only_closed_egress_is_served(self, backend, sbx):
        with pytest.raises(ValueError, match="CLOSED"):
            asyncio.run(
                backend.acquire(KEY, _spec(egress=Egress.ALLOWLIST, egress_allow=("a.example",)))
            )

    def test_a_warm_acquire_adopts_and_refuses_a_changed_base(self, backend, sbx):
        first = asyncio.run(backend.acquire(KEY, _spec()))
        creates = sum(call[0] == "create" for call in sbx.calls)
        again = asyncio.run(backend.acquire(KEY, _spec()))
        assert again.instance_id == first.instance_id
        assert sum(call[0] == "create" for call in sbx.calls) == creates
        with pytest.raises(ValueError, match="dispose it"):
            asyncio.run(backend.acquire(KEY, _spec(work_dir="/srv/other")))

    def test_a_create_conflict_the_listing_missed_is_a_daemon_fault(self, backend, sbx):
        async def conflicted(*args: str, timeout: float | None = None) -> _Result:
            if args[0] == "create":
                return _Result(1, b"", b"error: sandbox 'x' already exists\n")
            return await FakeSbx.__call__(sbx, *args, timeout=timeout)

        backend._sbx = conflicted  # type: ignore[method-assign]
        with pytest.raises(SbxDaemonFault, match="sbx daemon restart"):
            asyncio.run(backend.acquire(KEY, _spec()))


class TestExec:
    def test_sbx_chatter_before_the_nonce_is_not_the_commands_stderr(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))

        def restarted(args: tuple[str, ...]) -> _Result:
            nonce = args[6]
            stderr = f"Sandbox x started successfully\n{nonce}\nguest err\n".encode()
            return _Result(3, b"out", stderr)

        sbx.exec_hook = restarted
        result = asyncio.run(sandbox.exec(["x", ""], working_directory=".", timeout=10))
        assert (result.stdout_bytes, result.stderr_bytes, result.exit_code) == (
            b"out",
            b"guest err\n",
            3,
        )

    def test_no_nonce_means_the_wrapper_never_ran(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        sbx.exec_hook = lambda _args: _Result(1, b"", b"error: sandbox 'x' not found\n")
        with pytest.raises(SbxError, match="not found"):
            asyncio.run(sandbox.exec(["true"], working_directory=".", timeout=10))
        sbx.exec_hook = lambda _args: _Result(1, b"", b"500: backend unavailable\n")
        with pytest.raises(SbxDaemonFault):
            asyncio.run(sandbox.exec(["true"], working_directory=".", timeout=10))

    def test_a_missing_mount_is_bound_again_and_the_command_retried(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        sbx.unmounted_once = True
        before = len(sbx.calls)
        result = asyncio.run(sandbox.exec(["true"], working_directory=".", timeout=10))
        assert result.stdout == "ran"
        remounts = [call for call in sbx.calls[before:] if _MOUNT_SCRIPT in call]
        assert len(remounts) == 1 and remounts[0][-1] == "again"

    def test_a_timeout_kills_the_process_group_and_raises(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))

        def slow(_args: tuple[str, ...]) -> _Result:
            raise TimeoutError

        sbx.exec_hook = slow
        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["sleep", "9"], working_directory=".", timeout=1))
        assert sbx.calls[-1][:5] == ("exec", sandbox.name, "sh", "-c", _KILL_SCRIPT)


class TestRemoval:
    def _removal_argv(self, sbx: FakeSbx) -> list[str]:
        call = next(call for call in reversed(sbx.calls) if _EXEC_SCRIPT in call)
        return [_decode(item) for item in call[10:]]

    def test_a_non_recursive_removal_never_recurses_in_the_guest(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        asyncio.run(sandbox.write_file("f", b"x", working_directory="."))
        asyncio.run(sandbox.remove("f", working_directory="."))
        assert self._removal_argv(sbx) == ["rm", "-f", "--", "/maf-sandbox/work/f"]

    def test_recursive_removal_and_reclaim_recurse(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        asyncio.run(sandbox.write_file("d/f", b"x", working_directory="."))
        asyncio.run(sandbox.remove("d", working_directory=".", recursive=True))
        assert self._removal_argv(sbx) == ["rm", "-rf", "--", "/maf-sandbox/work/d"]
        asyncio.run(sandbox.reclaim("d", working_directory=".", timeout=10))
        assert self._removal_argv(sbx) == ["rm", "-rf", "--", "/maf-sandbox/work/d"]


class TestDeadlines:
    def test_a_remount_spends_the_callers_deadline(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        sbx.unmounted_once = True
        before = len(sbx.calls)
        asyncio.run(sandbox.exec(["true"], working_directory=".", timeout=5))
        remount = next(
            index for index in range(before, len(sbx.calls)) if _MOUNT_SCRIPT in sbx.calls[index]
        )
        bound = sbx.timeouts[remount]
        assert bound is not None and bound <= 5

    def test_a_remount_that_overruns_the_deadline_is_the_callers_timeout(self, backend, sbx):
        sandbox = asyncio.run(backend.acquire(KEY, _spec()))
        sbx.unmounted_once = True
        real = sbx.__call__

        async def slow_mount(*args: str, timeout: float | None = None) -> _Result:
            if _MOUNT_SCRIPT in args:
                raise TimeoutError
            return await real(*args, timeout=timeout)

        backend._sbx = slow_mount  # type: ignore[method-assign]
        with pytest.raises(TimeoutError, match="within 5 seconds"):
            asyncio.run(sandbox.exec(["true"], working_directory=".", timeout=5))


class TestLocks:
    def test_the_lock_table_holds_only_names_in_use(self, backend, sbx):
        asyncio.run(backend.acquire(KEY, _spec()))
        assert asyncio.run(backend.dispose(KEY)) is None
        assert backend._locks == {}

    def test_concurrent_acquires_of_one_name_create_once(self, backend, sbx):
        async def both():
            return await asyncio.gather(
                backend.acquire(KEY, _spec()), backend.acquire(KEY, _spec())
            )

        first, second = asyncio.run(both())
        assert first.instance_id == second.instance_id
        assert sum(call[0] == "create" for call in sbx.calls) == 1
        assert backend._locks == {}


class TestDisposal:
    def test_dispose_removes_every_kind_and_its_workspace(self, backend, sbx, tmp_path):
        asyncio.run(backend.acquire(KEY, _spec()))
        asyncio.run(backend.acquire(KEY, SandboxSpec(kind="second")))
        assert asyncio.run(backend.dispose(KEY)) is None
        assert sbx.sandboxes == {} and list((tmp_path / "root").iterdir()) == []

    def test_a_workspace_the_listing_omits_is_still_removed(self, backend, sbx, tmp_path):
        asyncio.run(backend.acquire(KEY, _spec()))
        sbx.sandboxes.clear()
        purge = asyncio.run(backend.dispose_scope(KEY.scope, KEY.thread_id))
        assert purge.undisposed is None and purge.disposed == 1
        assert list((tmp_path / "root").iterdir()) == []

    def test_a_lost_engine_is_unreachable_and_keeps_the_workspace(self, backend, sbx, tmp_path):
        asyncio.run(backend.acquire(KEY, _spec()))
        sbx.rm_result = _Result(1, b"", b"500 Internal: backend unavailable\n")
        failure = asyncio.run(backend.dispose(KEY))
        assert failure is not None and failure.code == "unreachable"
        assert len(list((tmp_path / "root").iterdir())) == 1

    def test_a_stale_instance_id_removes_nothing(self, backend, sbx):
        asyncio.run(backend.acquire(KEY, _spec()))
        assert asyncio.run(backend.dispose(KEY, kind="kind", instance_id="old")) is None
        assert len(sbx.sandboxes) == 1

    def test_a_failed_listing_is_reported(self, backend, sbx):
        async def unlisted(*args: str, timeout: float | None = None) -> _Result:
            return _Result(1, b"", b"error: daemon down\n")

        backend._sbx = unlisted  # type: ignore[method-assign]
        failure = asyncio.run(backend.dispose(KEY))
        assert failure is not None and failure.code == "unlisted"


_SH = shutil.which("sh")
_HAS_TOOLS = _SH is not None and all(shutil.which(tool) for tool in ("setsid", "base64"))


@pytest.mark.skipif(not _HAS_TOOLS, reason="needs sh, setsid and base64")
class TestTheWrapperInARealShell:
    """The exec script itself, run by the host's own ``sh`` in place of the guest's."""

    def _run(self, tmp_path: Path, argv: list[str], *, mounted: bool = True, cwd: str = "/"):
        marker = tmp_path / "marker"
        if mounted:
            marker.touch()
        encoded = ["x" + base64.b64encode(value.encode()).decode() for value in (cwd, *argv)]
        return subprocess.run(
            [_SH or "sh", "-c", _EXEC_SCRIPT, "maf-sbx", "NONCE", str(tmp_path / "pg"), str(marker)]
            + encoded,
            capture_output=True,
            timeout=30,
        )

    def test_argv_arrives_verbatim_including_empty_arguments(self, tmp_path):
        result = self._run(tmp_path, ["printf", "[%s]", "", "a b", "$HOME", "x\ny\n", ""])
        assert result.stdout == b"[][a b][$HOME][x\ny\n][]"
        assert result.stderr == b"NONCE\n" and result.returncode == 0

    def test_streams_and_exit_codes_are_the_commands(self, tmp_path):
        result = self._run(tmp_path, ["sh", "-c", "echo out; echo err >&2; exit 7"])
        assert (result.stdout, result.stderr, result.returncode) == (b"out\n", b"NONCE\nerr\n", 7)
        assert not (tmp_path / "pg").exists()

    def test_a_missing_working_directory_exits_125(self, tmp_path):
        result = self._run(tmp_path, ["pwd"], cwd=str(tmp_path / "absent"))
        assert result.returncode == 125 and result.stderr.startswith(b"NONCE\n")

    def test_nothing_runs_while_the_mount_is_missing(self, tmp_path):
        result = self._run(tmp_path, ["touch", str(tmp_path / "ran")], mounted=False)
        assert result.stderr == b"NONCE-unmounted\n" and not (tmp_path / "ran").exists()
