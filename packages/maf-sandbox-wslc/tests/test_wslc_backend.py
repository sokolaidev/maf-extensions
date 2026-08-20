"""Offline tests for the wslc backend.

No WSL and no container: the one seam every ``wslc`` invocation goes through is replaced by
a fake that records argv and replays canned results, so what these tests pin is the command
line this backend actually builds.  Some tests reach the real seam anyway — with
``sys.executable`` standing in for ``wslc.exe`` — because the subprocess handling itself
(decoding, exit codes, killing a real child on timeout and on cancellation) is the one part a
fake cannot prove, and one reads a captured payload from a real ``wslc``, because a listing
this file invented agrees with the code that reads it by construction.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import tarfile
import time
from collections.abc import Sequence

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    ExecResult,
    Isolation,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)

from maf_sandbox_wslc import BACKEND_NAME, WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import (
    _container_name,
    _network_name,
    _proxy_name,
    _WslcResult,
)

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY, _SPEC.kind)
_WORK = "/maf-sandbox/work"


class _Recorded:
    def __init__(self, args: tuple[str, ...], stdin: bytes | None, timeout: float | None) -> None:
        self.args = args
        self.stdin = stdin
        self.timeout = timeout


class _FakeWslc:
    """Stands in for `WslcSandboxBackend._wslc`."""

    def __init__(self, responder=None) -> None:
        self.calls: list[_Recorded] = []
        self._responder = responder or (
            lambda args: (
                _WslcResult(1, b"", b"no such file")
                if args[:2] == ("container", "cp") and args[2] != "-"
                else _WslcResult(0, b"", b"")
            )
        )

    async def __call__(self, *args: str, stdin=None, timeout=None, read_limit=None) -> _WslcResult:
        self.calls.append(_Recorded(args, stdin, timeout))
        result = self._responder(args)
        if (
            args[:2] == ("container", "cp")
            and args[2] != "-"
            and result.returncode == 0
            and not result.stdout
        ):
            return _WslcResult(1, b"", b"no such file")
        return result

    def matching(self, *prefix: str) -> list[_Recorded]:
        return [c for c in self.calls if c.args[: len(prefix)] == prefix]

    def only(self, *prefix: str) -> _Recorded:
        found = self.matching(*prefix)
        if prefix == ("container", "cp"):
            found = [call for call in found if len(call.args) > 2 and call.args[2] == "-"]
        assert len(found) == 1, [c.args for c in self.calls]
        return found[0]


def _machine(
    running: Sequence[str] = (),
    stopped: Sequence[str] = (),
    overrides: dict[tuple[str, ...], _WslcResult] | None = None,
):
    """A responder describing which containers exist, and how a command answers."""

    def respond(args: tuple[str, ...]) -> _WslcResult:
        for prefix, result in (overrides or {}).items():
            if args[: len(prefix)] == prefix:
                return result
        if args[:2] == ("container", "list"):
            names = [*running, *stopped] if "-a" in args else list(running)
            if "--format" in args:
                payload = [{"Id": f"id-{n}", "Name": n} for n in names]
                return _WslcResult(0, json.dumps(payload).encode(), b"")
            return _WslcResult(0, "".join(f"id-{n}\n" for n in names).encode(), b"")
        if args[:2] == ("container", "logs"):
            return _WslcResult(0, b"listening on 3128\n", b"")
        return _WslcResult(0, b"", b"")

    return respond


def _explodes(args: tuple[str, ...]) -> _WslcResult:
    raise RuntimeError("wslc is not installed")


def _backend_with(responder=None, config=None) -> tuple[WslcSandboxBackend, _FakeWslc]:
    """A backend whose every wslc invocation goes to the fake, via the one protected seam."""
    backend = WslcSandboxBackend(config or WslcSandboxConfig())
    fake = _FakeWslc(responder)
    backend._wslc = fake  # type: ignore[method-assign]
    return backend, fake


# ---------------------------------------------------------------------------
# Backend identity — read by the router's isolation floor and capability match
# ---------------------------------------------------------------------------


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(WslcSandboxBackend(WslcSandboxConfig()), SandboxBackend)

    def test_declares_container_isolation(self):
        """The default `microvm` floor refuses this backend because of this value, by design.

        Superseded by the two-axis floor (#85): was `deployed=True`, now `min_isolation`.
        """
        assert WslcSandboxBackend(WslcSandboxConfig()).isolation == Isolation.CONTAINER

    def test_declares_closed_egress(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).egress == Egress.CLOSED

    def test_declares_exec_and_files_in_only(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).capabilities == frozenset(
            {Capability.EXEC, Capability.FILES_IN}
        )

    def test_is_named_wslc(self):
        # The literal, on purpose. `name == BACKEND_NAME` below pins them to each other and
        # would stay green if both moved together — and both moving together is precisely the
        # change that silently breaks every host with `selected="wslc"` in its configuration.
        assert WslcSandboxBackend(WslcSandboxConfig()).name == "wslc"

    def test_the_exported_constant_is_the_name_the_backend_answers_to(self):
        """#411: the value exists without building a backend, and cannot drift from it."""
        assert BACKEND_NAME == WslcSandboxBackend(WslcSandboxConfig()).name

    def test_selecting_by_the_constant_resolves_to_this_backend(self):
        """What the constant is for, exercised rather than asserted.

        `selected=` is a string match against `.name`, so this is the only test that would fail
        if the constant were right and the property were reading something else.
        """
        backend = WslcSandboxBackend(WslcSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, selected=BACKEND_NAME)
        assert router.backend is backend


class TestRouterFloor:
    """The single most important behavior change for this backend's users."""

    def test_the_default_floor_refuses_this_backend(self):
        with pytest.raises(SandboxBackendNotPermitted):
            SandboxRouter([WslcSandboxBackend(WslcSandboxConfig())])

    def test_opting_the_floor_down_to_container_admits_it(self):
        router = SandboxRouter(
            [WslcSandboxBackend(WslcSandboxConfig())], min_isolation=Isolation.CONTAINER
        )
        assert router.enabled


# ---------------------------------------------------------------------------
# acquire — create
# ---------------------------------------------------------------------------


class TestAcquireCreatesClosed:
    def test_the_container_gets_no_network(self):
        """`Egress.CLOSED` is this flag and nothing else — drop it and the claim is false."""
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))

        args = fake.only("container", "run").args
        assert "--network" in args
        assert args[args.index("--network") + 1] == "none"

    def test_the_container_is_detached_and_named(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))

        args = fake.only("container", "run").args
        assert args[:5] == ("container", "run", "-d", "--name", _NAME)

    def test_the_name_is_derived_from_the_key_and_the_kind(self):
        assert _container_name(_KEY, "bicep") == _container_name(
            SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer"), "bicep"
        )
        assert _container_name(_KEY, "bicep") != _container_name(
            SandboxKey(scope="scope-b", thread_id="thread-1", agent_dir="devops-engineer"), "bicep"
        )

    def test_two_kinds_on_one_key_get_two_containers(self):
        """A sandbox carries its spec's image and egress, so serving two kinds from one
        container would run the second workload under the first one's network policy."""
        assert _container_name(_KEY, "bicep") != _container_name(_KEY, "codeact")

    def test_the_keepalive_command_is_the_image_then_sleep_infinity(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert fake.only("container", "run").args[-3:] == (
            "bicep-sandbox:local",
            "sleep",
            "infinity",
        )

    def test_a_pinned_image_id_wins_over_the_reference(self):
        backend, fake = _backend_with(_machine())
        spec = SandboxSpec(kind="bicep", image="ignored:1", image_id="sha256:abc")
        asyncio.run(backend.acquire(_KEY, spec))

        assert fake.only("container", "run").args[-3:] == ("sha256:abc", "sleep", "infinity")

    def test_no_image_at_all_is_refused(self):
        backend, _ = _backend_with(_machine())
        with pytest.raises(ValueError, match="image"):
            asyncio.run(backend.acquire(_KEY, SandboxSpec(kind="bicep")))

    def test_labels_carry_the_key_and_the_specs_own_labels(self):
        backend, fake = _backend_with(_machine())
        spec = SandboxSpec(kind="bicep", image="i:1", labels={"kind": "bicep"})
        asyncio.run(backend.acquire(_KEY, spec))

        args = fake.only("container", "run").args
        labels = [args[i + 1] for i, a in enumerate(args) if a == "-l"]
        assert labels == [
            "maf-sandbox.scope=scope-a",
            "maf-sandbox.thread=thread-1",
            "maf-sandbox.agent=devops-engineer",
            "maf-sandbox.kind=bicep",
            "maf-sandbox.label.kind=bicep",
        ]

    def test_label_values_are_sanitized_at_create(self):
        backend, fake = _backend_with(_machine())
        key = SandboxKey(scope="user-" + "z" * 90, thread_id="thread-1", agent_dir="devops")
        asyncio.run(backend.acquire(key, _SPEC))

        args = fake.only("container", "run").args
        scope_label = next(args[i + 1] for i, a in enumerate(args) if a == "-l")
        assert scope_label.startswith("maf-sandbox.scope=sha256-")

    def test_creation_is_logged(self, caplog):
        backend, _ = _backend_with(_machine())
        with caplog.at_level(logging.INFO, logger="maf_sandbox_wslc"):
            asyncio.run(backend.acquire(_KEY, _SPEC))

        assert any("sandbox created" in r.getMessage() for r in caplog.records), caplog.text


# ---------------------------------------------------------------------------
# acquire — reuse
# ---------------------------------------------------------------------------


class TestAcquireReuses:
    def test_a_running_container_is_neither_created_nor_started(self):
        """A fix-round loop would otherwise pay a cold create every iteration."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert fake.matching("container", "run") == []
        assert fake.matching("container", "start") == []

    def test_reuse_is_logged(self, caplog):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        with caplog.at_level(logging.INFO, logger="maf_sandbox_wslc"):
            asyncio.run(backend.acquire(_KEY, _SPEC))

        assert any("sandbox reused" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_stopped_container_is_started_rather_than_replaced(self):
        backend, fake = _backend_with(_machine(stopped=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert fake.only("container", "start").args == ("container", "start", _NAME)
        assert fake.matching("container", "run") == []

    def test_a_missing_container_is_created(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert len(fake.matching("container", "run")) == 1
        assert fake.matching("container", "start") == []

    def test_a_container_that_will_not_start_is_replaced(self):
        """The name is taken, so the replacement has to remove it before `run` can reuse it."""
        overrides = {("container", "start"): _WslcResult(1, b"", b"WSLC_E_CONTAINER_CORRUPT")}
        backend, fake = _backend_with(_machine(stopped=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert fake.only("container", "remove").args == ("container", "remove", "-f", _NAME)
        assert len(fake.matching("container", "run")) == 1

    def test_a_name_that_only_shares_a_prefix_is_not_mistaken_for_a_match(self):
        """`--filter name=` is a substring match, so the listing is compared by exact name."""
        backend, fake = _backend_with(_machine(running=[_NAME + "-other"]))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert len(fake.matching("container", "run")) == 1

    def test_the_listing_is_filtered_by_name(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        args = fake.only("container", "list").args
        assert args[:2] == ("container", "list")
        assert "--format" in args and args[args.index("--format") + 1] == "json"
        assert f"name={_NAME}" in args


class TestAcquireRecoversFromANameConflict:
    """The listing that sent `acquire` down the create branch can be stale by the time `run` runs.

    Two acquires for one key race, or a transient listing failure hides a container that is
    right there. The name is derived from the key, so it stays taken: without a fallback every
    acquire for that key fails from here on, and the conversation loses its sandbox for good.
    """

    def _racing(self, *, running_after_the_conflict: bool):
        """A machine where `run` loses the name to a container that appears just before it."""
        present: list[str] = []

        def respond(args: tuple[str, ...]) -> _WslcResult:
            if args[:2] == ("container", "list"):
                if "--format" in args:
                    payload = [{"Id": f"id-{n}", "Name": n} for n in present]
                    return _WslcResult(0, json.dumps(payload).encode(), b"")
                return _WslcResult(0, "".join(f"id-{n}\n" for n in present).encode(), b"")
            if args[:2] == ("container", "run"):
                if running_after_the_conflict:
                    present.append(_NAME)
                return _WslcResult(1, b"", b"Error code: ERROR_ALREADY_EXISTS")
            return _WslcResult(0, b"", b"")

        return _backend_with(respond)

    def test_the_existing_container_is_used_instead_of_failing(self):
        backend, fake = self._racing(running_after_the_conflict=True)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        assert sandbox.container_name == _NAME
        assert len(fake.matching("container", "run")) == 1

    def test_the_fallback_is_tried_once_and_then_gives_up(self):
        """A name conflict with nothing behind it is a real failure, not a retry loop."""
        backend, fake = self._racing(running_after_the_conflict=False)

        with pytest.raises(RuntimeError, match="ERROR_ALREADY_EXISTS"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert len(fake.matching("container", "run")) == 1

    def test_any_other_create_failure_still_raises(self):
        overrides = {("container", "run"): _WslcResult(1, b"", b"WSLC_E_IMAGE_NOT_FOUND")}
        backend, _ = _backend_with(_machine(overrides=overrides))

        with pytest.raises(RuntimeError, match="WSLC_E_IMAGE_NOT_FOUND"):
            asyncio.run(backend.acquire(_KEY, _SPEC))


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


class TestExecArgv:
    def test_a_sequence_reaches_the_container_verbatim_with_no_shell(self):
        """`wslc exec` takes argv natively, so nothing needs quoting and nothing may be
        re-interpreted: an element containing `;` stays one argument."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        argv = ["echo", "a; rm -rf /", "$(id)"]
        asyncio.run(sandbox.exec(argv, working_directory="/maf-sandbox/work", timeout=5))

        args = fake.only("container", "exec").args
        assert args == ("container", "exec", "-w", "/maf-sandbox/work", _NAME, *argv)
        assert "sh" not in args

    def test_a_string_is_run_by_a_shell(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.exec("bicep build x || true", working_directory="/w", timeout=5))

        assert fake.only("container", "exec").args == (
            "container",
            "exec",
            "-w",
            "/w",
            _NAME,
            "sh",
            "-c",
            "bicep build x || true",
        )

    def test_the_per_call_timeout_reaches_the_seam(self):
        """Not the lifecycle timeout: a workload's own bound is what governs its command."""
        config = WslcSandboxConfig(command_timeout_seconds=60.0)
        backend, fake = _backend_with(_machine(running=[_NAME]), config=config)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.exec(["true"], working_directory="/w", timeout=12.5))

        assert fake.only("container", "exec").timeout == 12.5


class TestExecResult:
    def test_stdout_stderr_and_exit_code_are_mapped_verbatim(self):
        overrides = {("container", "exec"): _WslcResult(7, b"out\n", b"err\n")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        result = asyncio.run(sandbox.exec(["false"], working_directory="/w", timeout=5))

        assert result == ExecResult(stdout="out\n", stderr="err\n", exit_code=7)


class TestExecDiscardsATimedOutSandbox:
    """Killing `wslc exec` on the host does not reach the process it started in the container.

    There is no per-command handle to kill either, so the command runs on — holding the work
    directory and the CPU the next exec wants. Removing the container is the only reach there
    is, and a fresh one costs about the half second a create costs anyway.
    """

    def _timing_out(self):
        base = _machine(running=[_NAME])

        def respond(args: tuple[str, ...]) -> _WslcResult:
            if args[:2] == ("container", "exec"):
                raise TimeoutError("wslc exec did not answer")
            return base(args)

        return _backend_with(respond)

    def test_a_timed_out_exec_removes_the_container(self):
        backend, fake = self._timing_out()
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["sleep", "600"], working_directory="/w", timeout=1))

        assert fake.only("container", "remove").args == ("container", "remove", "-f", _NAME)

    def test_the_timeout_still_reaches_the_caller(self):
        """The workload reports a hang as a diagnostic; swallowing it would report success."""
        backend, _ = self._timing_out()
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec("sleep 600", working_directory="/w", timeout=1))

    def test_a_removal_that_also_fails_does_not_mask_the_timeout(self):
        base = _machine(running=[_NAME])

        def respond(args: tuple[str, ...]) -> _WslcResult:
            if args[:2] in (("container", "exec"), ("container", "remove")):
                raise TimeoutError("wslc is not answering at all")
            return base(args)

        backend, _ = _backend_with(respond)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["sleep", "600"], working_directory="/w", timeout=1))


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def _sent(self, path: str, content: str) -> tuple[_Recorded, tarfile.TarFile]:
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(path, content, working_directory=_WORK))
        call = fake.only("container", "cp")
        assert call.stdin is not None
        return call, tarfile.open(fileobj=io.BytesIO(call.stdin), mode="r")

    def test_the_copy_targets_the_container_root(self):
        """A `cp` destination must already exist, and `/` is the only path that always does."""
        call, _ = self._sent("/maf-sandbox/work/main.bicep", "x")
        assert call.args == ("container", "cp", "-", f"{_NAME}:/")

    def test_the_entry_is_the_path_without_its_leading_slash(self):
        _, archive = self._sent("/maf-sandbox/work/r1/main.bicep", "x")
        assert archive.getnames() == ["maf-sandbox/work/r1/main.bicep"]

    def test_a_relative_path_is_left_alone(self):
        _, archive = self._sent("maf-sandbox/work/main.bicep", "x")
        assert archive.getnames() == ["maf-sandbox/work/maf-sandbox/work/main.bicep"]

    def test_the_content_round_trips_as_utf8(self):
        _, archive = self._sent("/maf-sandbox/work/main.bicep", "param naïve string\n")
        member = archive.extractfile("maf-sandbox/work/main.bicep")
        assert member is not None
        assert member.read().decode("utf-8") == "param naïve string\n"

    def test_bytes_are_written_as_given(self):
        """The protocol's ``write_file`` takes ``str | bytes`` — an in-door carrying a PNG or a
        spreadsheet needs bytes, and they must reach the tar entry unencoded. Raising
        ``AttributeError`` on ``bytes.encode`` here was the load-bearing half of #370."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        asyncio.run(
            sandbox.write_file("/maf-sandbox/work/diagram.png", payload, working_directory=_WORK)
        )

        call = fake.only("container", "cp")
        assert call.stdin is not None
        archive = tarfile.open(fileobj=io.BytesIO(call.stdin), mode="r")
        member = archive.extractfile("maf-sandbox/work/diagram.png")
        assert member is not None
        assert member.read() == payload

    def test_the_entry_is_readable(self):
        _, archive = self._sent("/maf-sandbox/work/main.bicep", "x")
        assert archive.getmember("maf-sandbox/work/main.bicep").mode == 0o644

    def test_a_failed_copy_raises(self):
        """A write that silently did nothing would surface as a compiler error about a file
        the workload believes it just wrote."""
        overrides = {("container", "cp", "-"): _WslcResult(1, b"", b"WSLC_E_PATH_NOT_FOUND")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(RuntimeError, match="WSLC_E_PATH_NOT_FOUND"):
            asyncio.run(
                sandbox.write_file("/maf-sandbox/work/main.bicep", "x", working_directory=_WORK)
            )

    def test_a_refused_path_never_reaches_the_copy_seam(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(ValueError):
            asyncio.run(sandbox.write_file("../escape", "x", working_directory=_WORK))
        assert fake.matching("container", "cp", "-") == []


# ---------------------------------------------------------------------------
# The pull surface — stat_file, read_file, list_dir
# ---------------------------------------------------------------------------


class TestPullSurfaceRefusal:
    """This backend declares neither FILES_OUT nor FILES_LIST, so the protocol says all three
    pull-surface methods may raise. They must *exist* and raise the documented refusal rather
    than be absent — an ``AttributeError`` from a missing method was the second half of #370,
    and it reads as unrelated to a ``write_file`` that just succeeded.
    """

    def _sandbox(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        return asyncio.run(backend.acquire(_KEY, _SPEC))

    def test_stat_file_raises_notimplementederror(self):
        sandbox = self._sandbox()
        with pytest.raises(NotImplementedError, match="FILES_OUT"):
            asyncio.run(sandbox.stat_file("/maf-sandbox/work/x", working_directory="/w"))

    def test_read_file_raises_notimplementederror(self):
        sandbox = self._sandbox()
        with pytest.raises(NotImplementedError, match="FILES_OUT"):
            asyncio.run(
                sandbox.read_file("/maf-sandbox/work/x", working_directory="/w", max_bytes=64)
            )

    def test_list_dir_raises_notimplementederror(self):
        sandbox = self._sandbox()
        with pytest.raises(NotImplementedError, match="FILES_OUT"):
            asyncio.run(sandbox.list_dir("/maf-sandbox/work", working_directory="/w"))


# ---------------------------------------------------------------------------
# dispose / dispose_scope
# ---------------------------------------------------------------------------


class TestDispose:
    def test_removes_the_container_by_name(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.dispose(_KEY))

        assert fake.only("container", "remove").args == ("container", "remove", "-f", _NAME)

    def test_release_is_logged(self, caplog):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        with caplog.at_level(logging.INFO, logger="maf_sandbox_wslc"):
            asyncio.run(backend.dispose(_KEY))

        assert any("sandbox released" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_container_that_is_already_gone_is_not_an_error(self, caplog):
        """A `remove` of a missing container exits 1 — judged by stderr, not by the code."""
        not_found = _WslcResult(1, b"", b"Error code: WSLC_E_CONTAINER_NOT_FOUND\n")
        backend, _ = _backend_with(_machine(overrides={("container", "remove"): not_found}))

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_wslc"):
            asyncio.run(backend.dispose(_KEY))
        assert caplog.records == []

    def test_never_raises(self):
        backend, _ = _backend_with(_explodes)
        asyncio.run(backend.dispose(_KEY))

    def test_the_fallback_reaches_every_kind_this_process_remembers(self):
        """One key may own one container per kind; a dispose with a failing listing must
        reclaim all of them, not whichever one a single-slot registry kept last."""
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops-engineer", "bicep")] = "name-bicep"
        backend._registry[("scope-a", "thread-1", "devops-engineer", "codeact")] = "name-codeact"

        asyncio.run(backend.dispose(_KEY))

        assert sorted(c.args[-1] for c in fake.matching("container", "remove")) == [
            "name-bicep",
            "name-codeact",
        ]
        assert backend._registry == {}


class TestDisposeScope:
    def test_selects_on_both_labels_and_on_stopped_containers_too(self):
        backend, fake = _backend_with(_machine(stopped=["a", "b"]))
        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        args = fake.only("container", "list").args
        assert args[:5] == ("container", "list", "-a", "--format", "json")
        assert args[5:] == (
            "--filter",
            "label=maf-sandbox.scope=scope-a",
            "--filter",
            "label=maf-sandbox.thread=thread-1",
        )

    def test_removes_every_listed_name_and_returns_the_count(self):
        backend, fake = _backend_with(_machine(stopped=["a", "b"]))

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 2
        assert [c.args[-1] for c in fake.matching("container", "remove")] == ["a", "b"]

    def test_nothing_to_purge_is_zero_not_an_error(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0

    def test_a_container_this_process_created_survives_a_failing_listing(self):
        """The labels are the source of truth; the registry is what is left when they fail."""
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops", "bicep")] = "name-x"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 1
        assert fake.only("container", "remove").args[-1] == "name-x"

    def test_another_scopes_container_is_left_alone(self):
        backend, fake = _backend_with(_machine())
        backend._registry[("scope-b", "thread-1", "devops", "bicep")] = "name-other"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0
        assert fake.matching("container", "remove") == []
        assert ("scope-b", "thread-1", "devops", "bicep") in backend._registry

    def test_a_failing_seam_degrades_to_zero_rather_than_raising(self):
        """A conversation delete must not fail because wslc is unavailable."""
        backend, _ = _backend_with(_explodes)
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0


# ---------------------------------------------------------------------------
# Label values
# ---------------------------------------------------------------------------


class TestLabelValues:
    def test_short_safe_values_are_left_readable(self):
        from maf_sandbox_wslc._backend import _label_value

        assert _label_value("scope-a") == "scope-a"
        assert _label_value("x" * 63) == "x" * 63

    def test_long_values_are_digested_within_the_limit(self):
        from maf_sandbox_wslc._backend import _LABEL_VALUE_MAX, _label_value

        out = _label_value("y" * 200)
        assert out.startswith("sha256-")
        assert len(out) <= _LABEL_VALUE_MAX

    def test_values_carrying_a_separator_are_digested(self):
        """A value with `=` or a space would split the `-l k=v` argument in two."""
        from maf_sandbox_wslc._backend import _label_value

        for raw in ("a=b", "a b", "a\nb", "user@example.com", ""):
            assert _label_value(raw).startswith("sha256-"), raw

    def test_a_value_already_shaped_like_a_digest_is_digested_too(self):
        """It is a legal short plain value, so passing it through would let a caller pick a
        scope that lands on the label some other scope's digest produced."""
        from maf_sandbox_wslc._backend import _label_value

        forged = _label_value("user-" + "z" * 90)
        assert len(forged) == 55
        assert _label_value(forged) != forged
        assert _label_value(forged).startswith("sha256-")

    def test_values_sharing_a_long_prefix_do_not_collide(self):
        """Truncation would map these together; these labels gate one conversation's purge."""
        from maf_sandbox_wslc._backend import _label_value

        assert _label_value("user-" + "z" * 90 + "AAAA") != _label_value(
            "user-" + "z" * 90 + "BBBB"
        )

    def test_create_and_purge_agree_on_the_label(self):
        """Transform one side only and purge selects nothing — silently, since "found none"
        and "there were none" are the same result."""
        long_scope = "user-" + "z" * 90
        backend, fake = _backend_with(_machine())
        key = SandboxKey(scope=long_scope, thread_id="thread-1", agent_dir="devops")
        asyncio.run(backend.acquire(key, _SPEC))
        written = [
            fake.only("container", "run").args[i + 1]
            for i, a in enumerate(fake.only("container", "run").args)
            if a == "-l"
        ][0]

        backend2, fake2 = _backend_with(_machine())
        asyncio.run(backend2.dispose_scope(long_scope, "thread-1"))
        list_args = fake2.only("container", "list").args
        queried = list_args[list_args.index("--filter") + 1]

        assert queried == f"label={written}"


# ---------------------------------------------------------------------------
# The seam itself — the part a fake cannot prove
# ---------------------------------------------------------------------------


class TestTheSeam:
    """`sys.executable` stands in for `wslc.exe`: same subprocess handling, no WSL needed."""

    def test_stdout_stderr_and_exit_code_come_back_as_raw_bytes(self):
        """The seam must not decode: a tar stream on stdout has to survive it untouched."""
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = (
            "import sys; sys.stdout.buffer.write('naïve'.encode()); "
            "sys.stderr.buffer.write('ünï'.encode()); sys.exit(3)"
        )
        result = asyncio.run(backend._wslc("-c", script, timeout=30))

        assert (result.returncode, result.stdout, result.stderr) == (
            3,
            "naïve".encode(),
            "ünï".encode(),
        )

    def test_stdout_text_decodes_leniently_rather_than_raising(self):
        """A malformed byte in a diagnostic must reach a log, not raise past it."""
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import sys; sys.stdout.buffer.write(b'ok \\xff ok')"
        result = asyncio.run(backend._wslc("-c", script, timeout=30))

        assert result.stdout == b"ok \xff ok"
        assert result.stdout_text == "ok � ok"

    def test_stdin_reaches_the_process(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import sys; sys.stdout.write(sys.stdin.buffer.read().decode())"
        result = asyncio.run(backend._wslc("-c", script, stdin=b"tar bytes", timeout=30))

        assert result.stdout == b"tar bytes"

    def test_a_bounded_read_caps_stdout_and_reaps_the_process(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import sys,time; sys.stdout.buffer.write(b'x' * 1000); sys.stdout.flush(); time.sleep(3600)"
        result = asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=30))

        assert len(result.stdout) == 64
        assert result.returncode != 0

    def test_a_bounded_read_timeout_kills_and_propagates(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import time; time.sleep(3600)"

        with pytest.raises(TimeoutError):
            asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=0.01))

    def test_a_cancelled_bounded_read_kills_and_propagates(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import time; time.sleep(3600)"

        async def scenario() -> None:
            task = asyncio.ensure_future(backend._wslc("-c", script, read_limit=64, timeout=60))
            await asyncio.sleep(0.1)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)

        asyncio.run(scenario())

    def test_a_timeout_kills_the_process_and_propagates(self, monkeypatch):
        """`TimeoutError` propagating is the workload's cue to report a hang as a diagnostic;
        killing is what keeps a hung command from outliving the call."""

        class _Hanging:
            def __init__(self) -> None:
                self.killed = False

            async def communicate(self, stdin=None):
                await asyncio.sleep(3600)
                return b"", b""

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> int:
                return -9

        process = _Hanging()

        async def _fake_exec(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        backend = WslcSandboxBackend(WslcSandboxConfig())

        with pytest.raises(TimeoutError):
            asyncio.run(backend._wslc("container", "exec", timeout=0.01))
        assert process.killed

    def test_a_loop_that_cannot_spawn_subprocesses_says_which_loop_is_needed(self, monkeypatch):
        """A selector loop raises `NotImplementedError()` — no message, no cause, nothing a
        log line or a model can act on. `ValueError` is the channel `maf_sandbox.maf` surfaces
        verbatim, so the sentence reaches whoever is enabling the feature."""

        async def _no_subprocess(*args, **kwargs):
            raise NotImplementedError

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_subprocess)
        backend = WslcSandboxBackend(WslcSandboxConfig())

        with pytest.raises(ValueError, match="event loop"):
            asyncio.run(backend.acquire(_KEY, _SPEC))


#: Appends to `sys.argv[1]` forever. A one-liner, so it survives being one argv element.
_HEARTBEAT = (
    "import itertools, sys, time; p = sys.argv[1]; "
    "[(open(p, 'a').write('.'), time.sleep(0.02)) for _ in itertools.count()]"
)


class TestTheSeamReapsARealChild:
    """A real process, killed for real — the part both the fake above and a mock cannot show.

    `wslc.exe` outliving the call that made it is invisible from inside the process that
    abandoned it: the coroutine raises on time, the logs read correctly, and the container
    keeps working. So these watch the child's own heartbeat file instead, and a leak shows up
    as a file that goes on growing after the call has already raised.
    """

    def _stopped_growing(self, beat) -> bool:
        first = beat.stat().st_size
        time.sleep(0.5)
        return beat.stat().st_size == first

    def test_a_timeout_kills_the_child(self, tmp_path):
        beat = tmp_path / "beat"
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))

        with pytest.raises(TimeoutError):
            asyncio.run(backend._wslc("-c", _HEARTBEAT, str(beat), timeout=1.5))

        assert beat.exists(), "the child never started, so this proves nothing"
        assert self._stopped_growing(beat)

    def test_a_cancelled_call_kills_the_child(self, tmp_path):
        """Cancellation arrives at the same await a timeout does, and used to leave the child
        running: the caller went away and nothing was left holding the handle."""
        beat = tmp_path / "beat"
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))

        async def scenario() -> None:
            task = asyncio.ensure_future(backend._wslc("-c", _HEARTBEAT, str(beat), timeout=60))
            await asyncio.sleep(1.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        assert beat.exists(), "the child never started, so this proves nothing"
        assert self._stopped_growing(beat)


class TestAgainstRealWslcOutput:
    """A verbatim ``container list --format json`` payload from wslc 2.9.4.0.

    Every other listing in this file is invented, and an invented listing agrees with the code
    that reads it — ``{"Id", "Name"}`` is a guess that happens to be right. Real output carries
    ``CreatedAt``, ``Image``, ``Ports`` and an integer ``State`` too, and the name arrives
    without the leading slash some container CLIs put there. Rename the field upstream and this
    fails in CI rather than only on a machine with WSL. Regenerate with a throwaway container:

        wslc container run -d --name maf-sandbox-wslc-<12 hex> --network none alpine:3 sleep infinity
        wslc container list --format json --filter name=<that name>
        wslc container remove -f <that name>
    """

    #: The name the captured container was created with — an exact match for the payload.
    _CAPTURED = "maf-sandbox-wslc-c63d0bd23ebf"

    def _payload(self) -> str:
        import pathlib

        fixture = pathlib.Path(__file__).parent / "fixtures" / "wslc-container-list-real.json"
        return fixture.read_text(encoding="utf-8")

    def _seam(self):
        payload = self._payload()
        return _backend_with(lambda args: _WslcResult(0, payload.encode("utf-8"), b""))

    def test_the_exact_name_is_found_in_real_output(self):
        backend, _ = self._seam()
        assert asyncio.run(backend._is_listed(self._CAPTURED, all_states=False)) is True

    def test_a_name_the_payload_does_not_carry_is_not_found(self):
        """`--filter name=` is a substring match, so a real payload can hold a longer name."""
        backend, _ = self._seam()
        assert asyncio.run(backend._is_listed(self._CAPTURED[:-4], all_states=True)) is False

    def test_the_row_carries_the_container_id_a_listing_consumer_reads(self):
        from maf_sandbox_wslc._backend import _listed_names

        rows = json.loads(self._payload())
        assert _listed_names(self._payload()) == [self._CAPTURED]
        assert len(rows[0]["Id"]) == 64
        assert rows[0]["Image"] == "alpine:3"


# ---------------------------------------------------------------------------
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `maf-sandbox`
#: puts `maf_sandbox` on the path. Anything not listed here is assumed to import under its
#: distribution name with hyphens turned to underscores.
_DISTRIBUTION_TO_IMPORT_NAME = {"maf-sandbox": "maf_sandbox"}


def _package_modules():
    """Every module in the installed `maf_sandbox_wslc`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox_wslc

    root = pathlib.Path(maf_sandbox_wslc.__file__).parent  # type: ignore[arg-type]
    return {path.stem: path for path in root.rglob("*.py")}


def _imported_top_levels(path):
    """The absolute top-level module names imported by the file at `path`."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # relative import — within this package, not a dependency
            top = (node.module or "").split(".")[0]
            if top:
                names.append(top)
    return names


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox_wslc` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox_wslc

    root = pathlib.Path(maf_sandbox_wslc.__file__).parents[2]  # type: ignore[arg-type]
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    with pyproject_path.open("rb") as fh:
        requirements = tomllib.load(fh)["project"]["dependencies"]

    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, f"unparseable dependency requirement: {requirement!r}"
        distribution = match.group(0)
        names.add(_DISTRIBUTION_TO_IMPORT_NAME.get(distribution, distribution.replace("-", "_")))
    return names


class TestOnlyDeclaredDependencies:
    """Every module here imports only the standard library, itself, or a declared dependency.

    Nothing else would notice a stray import: the workspace running this suite has every
    sibling package already importable, so it resolves fine here regardless of what it names.
    The first sign of trouble is a downstream consumer who installs the published wheel alone
    and gets an `ImportError` with no test pointing at the cause.
    """

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 3

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys as _sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox_wslc package — "
                "this check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(_sys.stdlib_module_names) | declared | {"maf_sandbox_wslc"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox_wslc modules import something outside the standard library, "
            f"the package itself, and pyproject.toml's declared dependencies: {offenders}. "
            "Either the import is a mistake, or the dependency belongs in pyproject.toml."
        )


class TestNoMafImport:
    """A backend is framework-agnostic: it speaks the protocol, never the host's framework.

    `agent-framework-core` is not a declared dependency, so `TestOnlyDeclaredDependencies`
    already catches it — this names the specific property, so a failure says what broke.
    """

    def test_the_backend_does_not_import_agent_framework(self):
        offenders = sorted(
            path.name
            for path in _package_modules().values()
            if "agent_framework" in _imported_top_levels(path)
        )
        assert offenders == [], (
            f"these maf_sandbox_wslc modules import agent_framework: {offenders}. A backend "
            "must be usable by a host that does not run Microsoft Agent Framework at all."
        )


# ---------------------------------------------------------------------------
# Allowlist egress — internal network + filtering proxy
# ---------------------------------------------------------------------------

_ALLOW_CONFIG = WslcSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
_ALLOW_SPEC = SandboxSpec(
    kind="bicep",
    image="bicep-sandbox:local",
    egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"),
)
# The allowlist folds into the name, so an allowlisted sandbox is a different container from a
# closed one for the same key — which is what stops a reuse from crossing egress modes.
_ALLOW_ID = "allow:" + ",".join(sorted(_ALLOW_SPEC.egress_allow))
_AL = _container_name(_KEY, _ALLOW_SPEC.kind, _ALLOW_ID)
_AL_NET = _network_name(_AL)
_AL_PROXY = _proxy_name(_AL)


def _run_named(fake: _FakeWslc, name: str) -> _Recorded:
    """The one `container run` call whose `--name` is `name`."""
    found = [
        c for c in fake.matching("container", "run") if c.args[c.args.index("--name") + 1] == name
    ]
    assert len(found) == 1, [c.args for c in fake.calls]
    return found[0]


class TestAllowlistTopology:
    """With `egress_proxy_image` set, `--network none` becomes an internal net plus a proxy."""

    def test_the_declaration_follows_the_configuration(self):
        assert _backend_with()[0].egress == Egress.CLOSED
        assert _backend_with(config=_ALLOW_CONFIG)[0].egress == Egress.ALLOWLIST

    def test_create_builds_network_proxy_bridge_then_workload_in_order(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        order = [
            fake.calls.index(fake.only("network", "create")),
            fake.calls.index(_run_named(fake, _AL_PROXY)),
            fake.calls.index(fake.only("network", "connect")),
            fake.calls.index(_run_named(fake, _AL)),
        ]
        assert order == sorted(order)
        assert fake.only("network", "connect").args == ("network", "connect", "bridge", _AL_PROXY)

    def test_the_network_is_internal_and_labelled(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        args = fake.only("network", "create").args
        assert args[:3] == ("network", "create", "--internal")
        assert args[-1] == _AL_NET
        labels = [args[i + 1] for i, a in enumerate(args) if a == "-l"]
        assert "maf-sandbox.scope=scope-a" in labels
        assert "maf-sandbox.thread=thread-1" in labels

    def test_the_proxy_carries_the_allowlist_and_the_role_label(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        args = _run_named(fake, _AL_PROXY).args
        assert args[args.index("--network") + 1] == _AL_NET
        env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert "MAF_SANDBOX_ALLOW=mcr.microsoft.com,*.data.mcr.microsoft.com" in env
        labels = [args[i + 1] for i, a in enumerate(args) if a == "-l"]
        assert "maf-sandbox.role=proxy" in labels
        assert args[-1] == "maf-egress-proxy:local"

    def test_the_workload_joins_the_network_with_the_proxy_in_its_environment(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        args = _run_named(fake, _AL).args
        assert args[args.index("--network") + 1] == _AL_NET
        env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert f"HTTPS_PROXY=http://{_AL_PROXY}:3128" in env
        assert f"HTTP_PROXY=http://{_AL_PROXY}:3128" in env
        assert args[-3:] == ("bicep-sandbox:local", "sleep", "infinity")

    def test_the_proxy_is_recreated_fresh_every_acquire(self):
        """Never adopted: a fresh proxy has this spec's allowlist, its bridge leg, a clean log."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        removed = fake.calls.index(fake.only("container", "remove"))
        assert fake.only("container", "remove").args[-1] == _AL_PROXY
        assert removed < fake.calls.index(_run_named(fake, _AL_PROXY))

    def test_create_waits_until_the_proxy_listens(self):
        logs_seen = 0

        def respond(args):
            nonlocal logs_seen
            if args[:2] == ("container", "logs"):
                logs_seen += 1
                if logs_seen < 3:
                    return _WslcResult(0, b"", b"")
            return _machine()(args)

        backend, fake = _backend_with(respond, config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        assert logs_seen == 3
        last_logs = max(i for i, c in enumerate(fake.calls) if c.args[:2] == ("container", "logs"))
        assert fake.calls.index(_run_named(fake, _AL)) > last_logs

    def test_an_existing_network_is_adopted(self):
        overrides = {
            ("network", "create"): _WslcResult(1, b"", b"Error code: ERROR_ALREADY_EXISTS")
        }
        backend, fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        assert _run_named(fake, _AL)

    def test_a_missing_proxy_image_error_names_the_build_recipe(self):
        def respond(args):
            if args[:2] == ("container", "run") and _AL_PROXY in args:
                return _WslcResult(1, b"", b"WSLC_E_IMAGE_NOT_FOUND")
            return _machine()(args)

        backend, fake = _backend_with(respond, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="wslc build"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        # The network it just made must not be left behind when the proxy cannot come up.
        assert fake.matching("network", "remove")[-1].args[-1] == _AL_NET

    def test_a_proxy_without_its_bridge_leg_is_a_hard_failure(self):
        """A proxy on the internal net but not bridged would silently enforce nothing."""

        def respond(args):
            if args[:3] == ("network", "connect", "bridge"):
                return _WslcResult(1, b"", b"E_FAIL")
            return _machine()(args)

        backend, _ = _backend_with(respond, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="outbound leg"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

    def test_a_proxy_that_never_listens_fails_the_acquire(self):
        """Better to fail than hand back a sandbox whose egress is not actually up."""
        import maf_sandbox_wslc._backend as backend_mod

        def respond(args):
            if args[:2] == ("container", "logs"):
                return _WslcResult(0, b"starting up\n", b"")  # never the readiness marker
            return _machine()(args)

        backend, fake = _backend_with(respond, config=_ALLOW_CONFIG)
        original = backend_mod._PROXY_READY_ATTEMPTS, backend_mod._PROXY_READY_DELAY_S
        backend_mod._PROXY_READY_ATTEMPTS, backend_mod._PROXY_READY_DELAY_S = 2, 0.0
        try:
            with pytest.raises(RuntimeError, match="never reported listening"):
                asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        finally:
            backend_mod._PROXY_READY_ATTEMPTS, backend_mod._PROXY_READY_DELAY_S = original
        # The network it created on the way in must be reclaimed on the failure.
        assert fake.matching("network", "remove")[-1].args[-1] == _AL_NET

    def test_closed_mode_issues_no_network_commands_at_all(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert fake.matching("network") == []

    def test_an_empty_allowlist_stays_closed(self):
        """Allow nothing is `--network none`, not a proxy that would allow the same nothing."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        spec = SandboxSpec(kind="bicep", image="i:1", egress_allow=())
        asyncio.run(backend.acquire(_KEY, spec))

        assert fake.matching("network") == []
        args = _run_named(fake, _NAME).args
        assert args[args.index("--network") + 1] == "none"


class TestAllowlistIdentity:
    """The egress folds into the container name so a reuse cannot cross egress boundaries."""

    def test_closed_and_allowlisted_names_differ(self):
        assert _container_name(_KEY, _SPEC.kind) != _container_name(
            _KEY, _ALLOW_SPEC.kind, _ALLOW_ID
        )

    def test_a_different_allowlist_is_a_different_sandbox(self):
        wider = "allow:" + ",".join(sorted((*_ALLOW_SPEC.egress_allow, "aka.ms")))
        assert _container_name(_KEY, _ALLOW_SPEC.kind, _ALLOW_ID) != _container_name(
            _KEY, _ALLOW_SPEC.kind, wider
        )

    def test_an_allowlist_backend_does_not_reuse_a_closed_container(self):
        # The closed container for this key is running; an allowlist acquire must still build
        # its own, because reusing the closed one would declare an allowlist over no egress.
        backend, fake = _backend_with(_machine(running=[_NAME]), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        assert _run_named(fake, _AL)
        assert fake.matching("container", "run")  # created, not reused


class TestAllowlistReuseRepairsEgress:
    """A warm workload does not mean a working proxy — a reboot stops the proxy, not the key."""

    def test_reuse_rebuilds_the_proxy_but_not_the_workload(self):
        backend, fake = _backend_with(_machine(running=[_AL]), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        assert _run_named(fake, _AL_PROXY)  # proxy rebuilt
        assert fake.matching("network", "connect")  # and reconnected to egress
        with pytest.raises(AssertionError):
            _run_named(fake, _AL)  # the workload itself was reused, not recreated

    def test_restart_rebuilds_the_proxy_too(self):
        backend, fake = _backend_with(_machine(stopped=[_AL]), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        assert _run_named(fake, _AL_PROXY)
        assert fake.matching("network", "connect")
        assert fake.matching("container", "start")  # the workload was started, not recreated


class TestAllowlistTeardown:
    def test_dispose_removes_the_listed_workload_and_proxy_then_the_network(self):
        backend, fake = _backend_with(_machine(stopped=[_AL, _AL_PROXY]), config=_ALLOW_CONFIG)
        asyncio.run(backend.dispose(_KEY))

        assert [c.args[-1] for c in fake.matching("container", "remove")] == [_AL, _AL_PROXY]
        assert fake.only("network", "remove").args == ("network", "remove", _AL_NET)
        containers_done = max(
            i for i, c in enumerate(fake.calls) if c.args[:2] == ("container", "remove")
        )
        assert fake.calls.index(fake.only("network", "remove")) > containers_done

    def test_dispose_sweeps_by_label_even_when_this_backend_is_closed(self):
        # B2: a backend now in closed config must still reclaim an allowlisted sandbox — proxy
        # and network included — that an earlier run left behind, found purely by its labels.
        backend, fake = _backend_with(_machine(stopped=[_AL, _AL_PROXY]))
        asyncio.run(backend.dispose(_KEY))

        assert set(c.args[-1] for c in fake.matching("container", "remove")) == {_AL, _AL_PROXY}
        assert [c.args[-1] for c in fake.matching("network", "remove")] == [_AL_NET]

    def test_closed_mode_dispose_removes_the_container_and_no_network(self):
        backend, fake = _backend_with(_machine(stopped=[_NAME]))
        asyncio.run(backend.dispose(_KEY))

        assert [c.args[-1] for c in fake.matching("container", "remove")] == [_NAME]
        assert fake.matching("network") == []

    def test_dispose_scope_counts_workloads_and_sweeps_their_proxies_networks(self):
        other = "maf-sandbox-wslc-feedfeedfeed"
        backend, fake = _backend_with(
            _machine(stopped=[_AL, _AL_PROXY, other]), config=_ALLOW_CONFIG
        )

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 2

        removed = [c.args[-1] for c in fake.matching("container", "remove")]
        assert removed[:3] == [_AL, _AL_PROXY, other]
        # `other` has no listed proxy, so its own proxy and network are still swept.
        assert _proxy_name(other) in removed
        assert set(c.args[-1] for c in fake.matching("network", "remove")) == {
            _AL_NET,
            _network_name(other),
        }

    def test_dispose_scope_registry_fallback_sweeps_the_proxy_and_network(self):
        # H2: when the listing fails, the remembered workload name must still take its proxy
        # and network with it, not just the workload.
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend._registry[("scope-a", "thread-1", "devops", "bicep")] = _AL

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 1
        removed = [c.args[-1] for c in fake.matching("container", "remove")]
        assert _AL in removed and _AL_PROXY in removed
        assert [c.args[-1] for c in fake.matching("network", "remove")] == [_AL_NET]
