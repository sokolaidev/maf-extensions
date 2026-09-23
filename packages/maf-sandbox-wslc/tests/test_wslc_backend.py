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
import base64
import gc
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tracemalloc
import weakref
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from maf_sandbox import (
    Capability,
    Cleanup,
    DisposalFailure,
    Egress,
    EgressObserved,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxOsFamilyNotSupported,
    SandboxRouter,
    SandboxSpec,
    ScopePurge,
)
from maf_sandbox.file_transfer import FileRefusal, shell_refusal

from maf_sandbox_wslc import BACKEND_NAME, WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import (
    _CREATE_AS_THE_GUEST,
    _CREATE_DIRECTORIES,
    _LEFT_TO_THE_GUEST,
    _NOT_FOUND,
    _PROXY_LOG_BYTES,
    _PROXY_LOG_TAIL,
    _WRITE_AS_THE_GUEST,
    _container_name,
    _egress_decisions,
    _network_name,
    _proxy_name,
    _sandbox_labels,
    _Sweep,
    _WslcResult,
    _WslcSandbox,
)
from maf_sandbox_wslc._probes import SETUP_COMMANDS, SETUP_PATH

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_id="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY, _SPEC.kind)
_WORK = "/maf-sandbox/work"
# Method tests prepare their own paths; lifecycle tests exercise the acquire contract.
_METHOD_SPEC = replace(_SPEC, requires=frozenset())


def _audit(action: str, host: str, *, error: str = "", method: str = "GET") -> str:
    return (
        json.dumps(
            {
                "msg": "request",
                "audit": {"action": action, "host": host, "method": method},
                "error": error,
            }
        )
        + "\n"
    )


def _cp_is_a_directory() -> _WslcResult:
    """What `container cp` answers for a directory source, measured on wslc 2.9.4.0.

    The message names no path, and `E_FAIL` is shared with a non-directory component, so it
    takes both lines to mean directory.
    """
    return _WslcResult(
        1,
        b"",
        (
            b"Cannot copy a directory to a file path. Use a directory target "
            b"(with trailing separator) instead.\r\nError code: E_FAIL\r\n"
        ),
    )


def _cp_path_not_found(source: str) -> _WslcResult:
    """What `container cp` answers for a missing guest path, measured on wslc 2.9.4.0.

    The sentence quotes the path the caller asked for, and the engine's own code sits under it.
    Modelled here rather than abbreviated because the wording is what a substring match would
    read: the path is caller-chosen text, and the code is not.
    """
    guest = source.split(":", 1)[1] if ":" in source else source
    return _WslcResult(
        1,
        b"",
        (
            f"Could not find the file {guest} in container {_NAME}\r\n"
            f"Error code: ERROR_PATH_NOT_FOUND\r\n"
        ).encode(),
    )


@pytest.mark.parametrize("state", ["cold", "warm", "stopped"])
def test_acquire_creates_a_missing_base_as_root_in_held_directories(state):
    machine = _machine(
        running=[_NAME] if state == "warm" else [],
        stopped=[_NAME] if state == "stopped" else [],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            ),
            ("container", "inspect"): _WslcResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": "instance",
                            "Config": {
                                "User": "10001:20001",
                                "Labels": {"maf-sandbox.work-dir.v1": _WORK},
                            },
                        }
                    ]
                ).encode(),
                b"",
            ),
        },
    )
    backend, fake = _backend_with(machine)
    asyncio.run(backend.acquire(_KEY, _SPEC))
    (created,) = _creations(fake)
    # One held command: the owner, the walk from `/`, then the missing directories.
    assert created.args == (
        "container",
        "exec",
        "--user",
        "0",
        "-w",
        "/",
        _NAME,
        "/bin/sh",
        "-c",
        _CREATE_DIRECTORIES,
        "sh",
        "10001:20001",
        "/",
        "--",
        "/maf-sandbox",
        _WORK,
    )
    assert not _guest_creations(fake)
    assert not fake.matching("container", "cp", "-")


def _left_to_the_guest(
    guest: _WslcResult | None = None,
) -> Callable[[tuple[str, ...]], _WslcResult]:
    """A responder whose root setup stops at a directory the image's user can write."""
    machine = _machine(
        running=[_NAME],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            )
        },
    )

    def respond(args: tuple[str, ...]) -> _WslcResult:
        if _CREATE_DIRECTORIES in args:
            return _WslcResult(0, f"{_LEFT_TO_THE_GUEST}\n".encode(), b"")
        if _CREATE_AS_THE_GUEST in args:
            return guest or _WslcResult(0, b"", b"")
        return machine(args)

    return respond


def test_a_base_root_may_not_create_is_created_by_the_image_user():
    backend, fake = _backend_with(_left_to_the_guest())
    asyncio.run(backend.acquire(_KEY, _SPEC))
    (created,) = _guest_creations(fake)
    # No `--user`: the image's user runs it, so its own permissions bound where it lands.
    assert created.args == (
        "container",
        "exec",
        "-w",
        "/",
        _NAME,
        "sh",
        "-c",
        _CREATE_AS_THE_GUEST,
        "sh",
        _WORK,
    )


@pytest.mark.parametrize(
    ("stderr", "error"),
    [
        (b"mkdir: cannot create directory '/maf-sandbox': Permission denied\n", PermissionError),
        (b"mkdir: cannot create directory '/maf-sandbox': Not a directory\n", NotADirectoryError),
        (b"mkdir: cannot create directory '/maf-sandbox': Input/output error\n", RuntimeError),
    ],
)
def test_the_image_users_refusal_to_create_the_base_raises_its_error(stderr, error):
    backend, _ = _backend_with(_left_to_the_guest(_WslcResult(1, b"", stderr)))
    with pytest.raises(error, match="as the image's user"):
        asyncio.run(backend.acquire(_KEY, _SPEC))


@pytest.mark.parametrize("capabilities", [{Capability.EXEC}, {Capability.FILES_IN}])
def test_an_image_user_without_mkdir_is_a_typed_refusal(capabilities):
    """No probe asks for ``mkdir`` on an ``EXEC``-only acquire, so the command does.

    The type is what tells a caller "this image cannot serve that" from a broken engine.
    """
    missing = _WslcResult(127, b"", b"maf-setup-missing mkdir\n")
    backend, _ = _backend_with(_left_to_the_guest(missing))
    spec = replace(_SPEC, requires=frozenset(capabilities))
    with pytest.raises(SandboxCapabilityNotSupported, match="mkdir for the image's user"):
        asyncio.run(backend.acquire(_KEY, spec))


@pytest.mark.parametrize("command", ["as root", "as the image's user"])
@pytest.mark.parametrize("ending", ["fails", "times out", "is cancelled"])
def test_a_setup_that_does_not_finish_takes_the_container_with_it(ending, command):
    """`acquire` returns no sandbox, so nothing else can dispose the container it made.

    Setup runs commands the host process cannot reach once it is killed, and the container
    stays registered for warm reuse, so a later acquire could race one.
    """
    machine = _machine(
        running=[],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            )
        },
    )
    ending_script = _CREATE_DIRECTORIES if command == "as root" else _CREATE_AS_THE_GUEST

    def respond(args):
        if command != "as root" and _CREATE_DIRECTORIES in args:
            return _WslcResult(0, _LEFT_TO_THE_GUEST.encode(), b"")
        if ending_script in args:
            if ending == "fails":
                return _WslcResult(1, b"", b"setup refused")
            raise (
                TimeoutError("setup timed out")
                if ending == "times out"
                else asyncio.CancelledError()
            )
        return machine(args)

    backend, fake = _backend_with(respond)
    with pytest.raises((RuntimeError, TimeoutError, asyncio.CancelledError)):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    assert [call.args for call in fake.matching("container", "remove")], "the container was kept"


def test_setup_cleanup_leaves_a_container_that_took_the_name_after_the_discard():
    """Setup discarded its own instance, so whatever holds that name now is not this one.

    Cleanup addresses the instance it captured. A key-wide sweep would select on labels a
    replacement carries too, and remove a container another host is using.
    """
    machine = _machine(
        running=[_NAME],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            )
        },
    )

    def respond(args):
        # The instance setup discarded is gone; the name is still listed, as a replacement
        # created between the discard and this cleanup would leave it.
        if args[:2] == ("container", "inspect") and args[-1] == f"id-{_NAME}":
            return _WslcResult(1, b"", b"WSLC_E_CONTAINER_NOT_FOUND")
        if _CREATE_DIRECTORIES in args:
            raise TimeoutError("setup timed out")
        return machine(args)

    backend, fake = _backend_with(respond)
    with pytest.raises(TimeoutError):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    removed = [call.args[-1] for call in fake.matching("container", "remove")]
    assert removed == [f"id-{_NAME}"], "cleanup reached past the instance it captured"


def test_a_guest_command_stopped_at_its_output_cap_discards_the_container():
    """Reaching ``read_limit`` kills the host process and returns; nothing raises on its own.

    The command inside the container keeps running, so it can still publish the file after
    the caller was told the write failed, and a warm acquire would reuse that container.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if _WRITE_AS_THE_GUEST in args:
            return _WslcResult(1, b"x" * 4096, b"")
        return machine(args)

    backend, fake = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(RuntimeError, match="may still be running inside the container"):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert [call.args for call in fake.matching("container", "remove")], "the container was kept"


def test_a_copy_that_fills_its_stdout_cap_is_not_a_guest_command():
    """``container cp`` is the host's own copy: a full cap is not an unclean guest command.

    Its stdout can legitimately carry a tar header, and killing the host ends the copy.
    """
    header = tarfile.TarInfo("sub/").tobuf()
    overrides = {("container", "cp"): _WslcResult(1, header, b"copy failed")}
    backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(RuntimeError, match="copy failed"):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert not fake.matching("container", "remove")


def test_an_exec_only_acquire_that_must_create_a_base_needs_the_image_user():
    """A base that has to be created has to be given to someone, EXEC included.

    The typed refusal is what a caller can act on; a bare RuntimeError reads as a broken
    engine rather than an image this backend cannot serve.
    """
    inspected = {
        "Id": "i",
        "Config": {"User": "worker", "Labels": {"maf-sandbox.work-dir.v1": _WORK}},
    }
    overrides = {
        ("container", "inspect"): _WslcResult(0, json.dumps([inspected]).encode(), b""),
        ("container", "exec", "-w", "/", _NAME, "id"): _WslcResult(1, b"", b"no id"),
    }
    backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
    spec = replace(_SPEC, requires=frozenset({Capability.EXEC}))
    with pytest.raises(SandboxCapabilityNotSupported, match="image user it would belong to"):
        asyncio.run(backend.acquire(_KEY, spec))


def test_an_existing_base_is_served_without_a_resolved_image_user():
    """Only creating a base needs the image user's numbers; a write stamps nothing."""
    inspected = {
        "Id": "i",
        "Config": {"User": "worker", "Labels": {"maf-sandbox.work-dir.v1": _WORK}},
    }
    overrides = {
        ("container", "cp", f"{_NAME}:{guest}"): _cp_is_a_directory()
        for guest in ("/", "/maf-sandbox", _WORK)
    }
    overrides[("container", "inspect")] = _WslcResult(0, json.dumps([inspected]).encode(), b"")
    overrides[("container", "exec", "-w", "/", _NAME, "id")] = _WslcResult(1, b"", b"no id")
    backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
    sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert not _creations(fake)
    asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert _only_write(fake).stdin == b"data"


@pytest.mark.parametrize("ending", ["times out", "fills the read cap"])
def test_a_probe_that_may_still_be_running_takes_the_container_with_it(ending):
    """A probe can be raised to root, and acquire returns nothing for anyone to dispose.

    An ordinary refusal — a probe that answered with the wrong status — keeps the container,
    because nothing is left running in it and the next acquire retries.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:2] == ("container", "exec") and "sh" in args:
            if ending == "times out":
                raise TimeoutError("the probe did not answer")
            return _WslcResult(0, b"x" * 1024, b"")
        return machine(args)

    backend, fake = _backend_with(respond)
    spec = replace(_SPEC, requires=frozenset({Capability.EXEC}))
    with pytest.raises(SandboxCapabilityNotSupported, match="did not complete"):
        asyncio.run(backend.acquire(_KEY, spec))
    assert [call.args for call in fake.matching("container", "remove")], "the container was kept"


def test_a_probe_that_answered_badly_keeps_the_container():
    """The retryable case: the image lacks a command, and nothing is running in there."""
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:2] == ("container", "exec") and "sh" in args:
            return _WslcResult(127, b"", b"not found")
        return machine(args)

    backend, fake = _backend_with(respond)
    spec = replace(_SPEC, requires=frozenset({Capability.EXEC}))
    with pytest.raises(SandboxCapabilityNotSupported, match="sh command probe exited"):
        asyncio.run(backend.acquire(_KEY, spec))
    assert not fake.matching("container", "remove")


@pytest.mark.parametrize(
    ("answer", "named"),
    [
        (_WslcResult(127, b"", b"maf-setup-missing mkdir"), "mkdir"),
        (_WslcResult(127, b"", b"maf-setup-missing chown"), "chown"),
        # Both scripts compare `pwd -P`, so a shell without it fails the comparison.
        (_WslcResult(127, b"", b"maf-setup-missing pwd"), "pwd"),
        # A shell the engine could not start never reaches the script, so no marker comes
        # back — only the runtime's own words and a status in the unstartable range.
        (
            _WslcResult(
                126,
                b"",
                b"OCI runtime exec failed: exec failed: unable to start container process: "
                b'exec: "/bin/sh": stat /bin/sh: no such file or directory: unknown',
            ),
            "/bin/sh",
        ),
    ],
)
def test_a_missing_setup_prerequisite_is_named_in_a_typed_refusal(answer, named):
    """Which one is missing decides where a reader looks, so the refusal has to say.

    A bare status cannot: 126 and 127 are also what an engine answers when it cannot start
    the shell at all, so the script marks its own answer and the engine's is read separately.
    """
    machine = _machine(
        running=[_NAME],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            )
        },
    )

    def respond(args):
        return answer if _CREATE_DIRECTORIES in args else machine(args)

    backend, _ = _backend_with(respond)
    with pytest.raises(SandboxCapabilityNotSupported, match=re.escape(named)):
        asyncio.run(backend.acquire(_KEY, _SPEC))


def test_a_container_a_discard_could_not_remove_is_not_reused():
    """Something may still be running in it, so a warm acquire must not hand it back."""
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:3] == ("container", "remove", "-f"):
            return _WslcResult(1, b"", b"device or resource busy")
        if _WRITE_AS_THE_GUEST in args:
            raise TimeoutError("the write did not answer")
        return machine(args)

    backend, fake = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert fake.only("container", "remove").args == ("container", "remove", "-f", f"id-{_NAME}")
    # The removal failed, so the next acquire refuses rather than reusing that container.
    with pytest.raises(RuntimeError, match="may still be running something"):
        asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))


def test_a_discard_forgets_what_the_container_had_answered():
    """Probe results belong to the container, so they cannot outlive an attempt to remove it."""
    machine = _machine(running=[_NAME])

    def respond(args):
        if _WRITE_AS_THE_GUEST in args:
            raise TimeoutError("the write did not answer")
        return machine(args)

    backend, _ = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert backend._command_probes.get(_NAME, ("", set()))[1], "probes were cached by acquire"
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert _NAME not in backend._command_probes


def test_a_container_left_half_prepared_by_a_failed_cleanup_is_not_reused():
    """Setup failed and the cleanup could not remove it, so setup may still be running.

    Disposal drops the registry entry, so without remembering the name nothing else knows
    this container is half-prepared — and the next acquire would list it and reuse it.
    """
    machine = _machine(
        running=[],
        overrides={
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_path_not_found(
                f"{_NAME}:/maf-sandbox"
            )
        },
    )

    def respond(args):
        if _CREATE_DIRECTORIES in args:
            return _WslcResult(1, b"", b"setup refused")
        if args[:3] == ("container", "remove", "-f"):
            return _WslcResult(1, b"", b"device or resource busy")
        return machine(args)

    backend, fake = _backend_with(respond)
    with pytest.raises(RuntimeError, match="could not create the working directory"):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    assert fake.matching("container", "remove"), "cleanup was attempted"
    with pytest.raises(RuntimeError, match="may still be running something"):
        asyncio.run(backend.acquire(_KEY, _SPEC))


@pytest.mark.parametrize("command", ["mkdir", "chown", "ls", "pwd"])
def test_every_command_the_root_script_runs_is_a_checked_prerequisite(command):
    """A command the script runs but never checks fails late, as a generic error.

    The prerequisite loop is what turns a missing one into a refusal that names it, so the
    checked list has to hold every command the script reaches for.
    """
    # Everything but the prerequisite loop itself, which names them all by construction.
    used = chr(10).join(
        line for line in _CREATE_DIRECTORIES.splitlines() if "command -v" not in line
    )
    assert f"{command} " in used, f"{command} is not run by the script"
    assert command in SETUP_COMMANDS, f"{command} is run but never checked"


def test_a_container_a_failed_probe_could_not_remove_is_not_reused():
    """The probe addresses the container by instance ID; reuse is decided by name.

    Quarantining the ID the removal used would record something the guard never looks for,
    so a probe that may still be running would be reused on the next acquire.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:2] == ("container", "exec") and "sh" in args:
            raise TimeoutError("the probe did not answer")
        if args[:3] == ("container", "remove", "-f"):
            return _WslcResult(1, b"", b"device or resource busy")
        return machine(args)

    backend, fake = _backend_with(respond)
    spec = replace(_SPEC, requires=frozenset({Capability.EXEC}))
    with pytest.raises(SandboxCapabilityNotSupported, match="did not complete"):
        asyncio.run(backend.acquire(_KEY, spec))
    assert fake.matching("container", "remove"), "the discard was attempted"
    # The name is what acquire looks for, so the guard has to have recorded that.
    assert _NAME in backend._undiscarded
    with pytest.raises(RuntimeError, match="may still be running something"):
        asyncio.run(backend.acquire(_KEY, spec))


def test_a_discard_quarantines_before_it_awaits_the_removal():
    """A discard runs outside the acquire lock, so a concurrent acquire has to see it.

    Recording only once the removal returns leaves a window where that acquire finds nothing
    and warm-reuses a container whose guest command is still running.
    """
    machine = _machine(running=[_NAME])
    during: list[dict[str, set[str]]] = []

    def respond(args):
        if args[:3] == ("container", "remove", "-f"):
            # Copied a level down: the sets are mutated in place when the entry is released.
            during.append({held: set(ids) for held, ids in backend._undiscarded.items()})
        if _WRITE_AS_THE_GUEST in args:
            raise TimeoutError("the write did not answer")
        return machine(args)

    backend, _ = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert during == [{_NAME: {f"id-{_NAME}"}}], "an acquire during the removal would see nothing"
    assert not backend._undiscarded, "a removal that succeeded clears it again"


def test_a_discard_does_not_clear_a_quarantine_a_later_one_recorded():
    """Two discards can overlap on one name, and only one of them removed this instance.

    Clearing the entry by name would release a container the other one is still holding back.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:3] == ("container", "remove", "-f"):
            # A second discard, quarantining a different instance under the same name.
            backend._quarantine(_NAME, "id-newer")
        if _WRITE_AS_THE_GUEST in args:
            raise TimeoutError("the write did not answer")
        return machine(args)

    backend, _ = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    assert backend._undiscarded == {_NAME: {"id-newer"}}


def test_two_discards_on_one_name_are_both_held():
    """An instance and the one that replaced it can be discarded at once under one name.

    A single slot would keep whichever registered last, and the other would be neither
    retried nor held back — reused on the next acquire with its guest command unaccounted for.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:3] == ("container", "remove", "-f"):
            return _WslcResult(1, b"", b"device or resource busy")
        return machine(args)

    backend, _ = _backend_with(respond)
    asyncio.run(backend._discard_container("id-first", _NAME))
    asyncio.run(backend._discard_container("id-second", _NAME))
    assert backend._undiscarded == {_NAME: {"id-first", "id-second"}}


def test_an_instance_quarantined_while_acquire_prepared_it_is_not_handed_back():
    """The drain is a read, not a fence: a discard can land while the acquire prepares.

    Returning the sandbox anyway hands the caller the container that entry holds back.
    """
    machine = _machine(running=[_NAME])

    def respond(args):
        if _CREATE_DIRECTORIES in args or args[-2:] == ("id", "-u"):
            # A discard of this very instance, landing after the drain read the name clear.
            backend._quarantine(_NAME, f"id-{_NAME}")
        return machine(args)

    backend, _ = _backend_with(respond)
    with pytest.raises(RuntimeError, match="quarantined while this acquire prepared"):
        asyncio.run(backend.acquire(_KEY, _SPEC))


def test_an_acquire_clears_a_quarantine_installed_while_it_cleared_the_last_one():
    """A discard runs outside the acquire lock, so the name can be quarantined again mid-retry.

    Keeping that newer entry is not a fence on its own: reusing the name with one standing
    hands back the very container it is holding back.
    """
    machine = _machine(running=[_NAME])
    installed: list[str] = []

    def respond(args):
        if args[:3] == ("container", "remove", "-f") and not installed:
            # A concurrent discard, quarantining a different instance under the same name.
            installed.append("id-newer")
            backend._quarantine(_NAME, "id-newer")
        return machine(args)

    backend, fake = _backend_with(respond)
    backend._quarantine(_NAME, f"id-{_NAME}")
    asyncio.run(backend.acquire(_KEY, _SPEC))
    assert [c.args[-1] for c in fake.matching("container", "remove")] == [f"id-{_NAME}", "id-newer"]
    assert not backend._undiscarded


def test_an_acquire_refuses_a_quarantine_it_cannot_drain():
    """Bounded: a name quarantined again on every pass is not something to spin on."""
    machine = _machine(running=[_NAME])
    seen = 0

    def respond(args):
        nonlocal seen
        if args[:3] == ("container", "remove", "-f"):
            seen += 1
            backend._quarantine(_NAME, f"id-newer-{seen}")
        return machine(args)

    backend, _ = _backend_with(respond)
    backend._quarantine(_NAME, f"id-{_NAME}")
    with pytest.raises(RuntimeError, match="quarantined again every time"):
        asyncio.run(backend.acquire(_KEY, _SPEC))


def test_the_quarantine_retry_removes_the_instance_it_quarantined_not_the_name():
    """Another host sharing the name can replace the instance before the retry runs.

    An instance ID only ever names the container this backend failed to remove, so the retry
    cannot reach a healthy replacement that took the name in the meantime.
    """
    machine = _machine(running=[_NAME])
    removals_fail = True

    def respond(args):
        if args[:3] == ("container", "remove", "-f") and removals_fail:
            return _WslcResult(1, b"", b"device or resource busy")
        if _WRITE_AS_THE_GUEST in args:
            raise TimeoutError("the write did not answer")
        return machine(args)

    backend, fake = _backend_with(respond)
    sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.write_file("input", b"data", working_directory=_WORK))
    # Keyed by the name acquire looks up, holding the instance the retry has to remove.
    assert backend._undiscarded == {_NAME: {f"id-{_NAME}"}}

    removals_fail = False
    asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    assert fake.matching("container", "remove")[-1].args[-1] == f"id-{_NAME}"
    assert not backend._undiscarded


def _writes(fake: _FakeWslc) -> list[_Recorded]:
    """Every write command the fake saw: one guest ``exec -i`` per ``write_file``."""
    return [call for call in fake.calls if _WRITE_AS_THE_GUEST in call.args]


def _only_write(fake: _FakeWslc) -> _Recorded:
    (write,) = _writes(fake)
    return write


def _creations(fake: _FakeWslc) -> list[_Recorded]:
    """Every working-directory setup command the fake saw."""
    return [call for call in fake.calls if _CREATE_DIRECTORIES in call.args]


def _guest_creations(fake: _FakeWslc) -> list[_Recorded]:
    """Every working-directory setup the fake saw run as the image's user."""
    return [call for call in fake.calls if _CREATE_AS_THE_GUEST in call.args]


def _operands(call: _Recorded) -> tuple[str, ...]:
    """A write's target, parent, staged sibling and byte count, in that order."""
    start = call.args.index(_WRITE_AS_THE_GUEST) + 2
    return call.args[start:]


#: The argv a guest-side stat probe arrives on: raised, and `test` passed as argv with no
#: shell. The fake matches overrides by prefix, so a key missing `--user 0` silently stops
#: matching and every probe falls through to the responder's default success.
_PROBE = ("container", "exec", "--user", "0", _NAME, "/usr/bin/test")


class _Recorded:
    def __init__(
        self,
        args: tuple[str, ...],
        stdin: bytes | None,
        timeout: float | None,
        read_limit: int | None = None,
    ) -> None:
        self.args = args
        self.stdin = stdin
        self.timeout = timeout
        self.read_limit = read_limit


class _FakeWslc:
    """Stands in for `WslcSandboxBackend._wslc`."""

    def __init__(self, responder=None) -> None:
        self.calls: list[_Recorded] = []
        self._responder = responder or _machine()

    async def __call__(self, *args: str, stdin=None, timeout=None, read_limit=None) -> _WslcResult:
        self.calls.append(_Recorded(args, stdin, timeout, read_limit))
        result = self._responder(args)
        if args[:2] == ("container", "inspect") and result == _WslcResult(0, b"", b""):
            result = _WslcResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": f"id-{args[-1]}",
                            "Config": {"User": "", "Labels": {"maf-sandbox.work-dir.v1": _WORK}},
                        }
                    ]
                ).encode(),
                b"",
            )
        return result

    def matching(self, *prefix: str) -> list[_Recorded]:
        return [c for c in self.calls if c.args[: len(prefix)] == prefix]

    def only(self, *prefix: str) -> _Recorded:
        found = [
            c
            for c in self.matching(*prefix)
            if not (c.args[-2:] in (("id", "-u"), ("id", "-g")) and c.read_limit == 64)
            and c.read_limit != 1024
        ]
        if prefix == ("container", "cp"):
            found = [call for call in found if len(call.args) > 2 and call.args[2] == "-"]
        assert len(found) == 1, [c.args for c in self.calls]
        return found[0]


def _json_lines(names: Sequence[str]) -> str:
    """A ``container list --format json`` payload in the shape wslc 2.9.12 emits.

    One object per line and the name under ``Names``, so every test driving this fake reads
    the listing the installed CLI writes rather than the array an earlier one did.
    """
    return "".join(json.dumps({"ID": f"id-{n}", "Names": n}) + "\n" for n in names)


def _machine(
    running: Sequence[str] = (),
    stopped: Sequence[str] = (),
    overrides: dict[tuple[str, ...], _WslcResult] | None = None,
    work_dir: str = _WORK,
):
    """A responder describing which containers exist, and how a command answers."""

    proxy_labels = {}
    storage_labels = {name: {"maf-sandbox.work-dir.v1": work_dir} for name in (*running, *stopped)}

    def respond(args: tuple[str, ...]) -> _WslcResult:
        for prefix, result in (overrides or {}).items():
            if args[: len(prefix)] == prefix:
                return result
        if args[:2] == ("container", "list"):
            names = [*running, *stopped] if "-a" in args else list(running)
            if "--format" in args:
                return _WslcResult(0, _json_lines(names).encode(), b"")
            return _WslcResult(0, "".join(f"id-{n}\n" for n in names).encode(), b"")
        if args[:2] == ("container", "cp") and args[2] != "-":
            # A copy *out* that nothing overrides is a path that is not there, and it says so
            # the way the engine does. Exit 0 with an empty stream is not spare capacity for
            # that: measured on wslc 2.9.4.0 it is how a regular file and a link answer, so a
            # test wanting either writes it and gets it.
            return _cp_path_not_found(args[2])
        if args[:2] == ("container", "run") and args[4].endswith("-proxy"):
            proxy_labels[args[4]] = dict(
                args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-l"
            )
        if args[:2] == ("container", "run"):
            storage_labels[args[4]] = dict(
                args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-l"
            )
        if args[:2] == ("container", "inspect") and args[-1].endswith("-proxy"):
            from maf_sandbox_wslc._backend import _sandbox_labels

            labels = proxy_labels.get(
                args[-1], {**_sandbox_labels(_KEY, _ALLOW_SPEC), "maf-sandbox.role": "proxy"}
            )
            return _WslcResult(
                0, json.dumps([{"Id": args[-1], "Config": {"Labels": labels}}]).encode(), b""
            )
        if args[:2] == ("container", "inspect"):
            # The engine resolves a name or an instance ID, and disposal addresses a
            # container by ID on purpose, so this fake has to answer both. Its IDs are
            # `id-<name>`; a real name wins over that shape.
            selector = args[-1]
            target = selector if selector in storage_labels else selector.removeprefix("id-")
            if target not in storage_labels:
                return _WslcResult(1, b"", b"WSLC_E_CONTAINER_NOT_FOUND")
            return _WslcResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": f"id-{target}",
                            "Name": f"/{target}",
                            "Config": {"User": ""},
                            "Labels": storage_labels.get(
                                target, {"maf-sandbox.work-dir.v1": work_dir}
                            ),
                        }
                    ]
                ).encode(),
                b"",
            )
        if args[:2] == ("container", "logs"):
            return _WslcResult(0, b"maf-sandbox egress contract v1\ntunnel proxy starting\n", b"")
        if args[:2] == ("container", "exec") and args[-2:] == (
            "cat",
            "/run/maf-proxy/ca.crt",
        ):
            return _WslcResult(0, b"-----BEGIN CERTIFICATE-----\nTEST\n", b"")
        if args[-2:-1] == ("-e",) and args[-1].startswith("/.maf-command-probe-"):
            return _WslcResult(1, b"", b"")
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


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("failure", ["status", "json", "shape", "empty", "raise"])
def test_failed_instance_inspection_disposes_the_container(warm, failure):
    machine = _machine(running=[_NAME] if warm else [])
    binding_checked = False

    def respond(args):
        nonlocal binding_checked
        if args[:2] == ("container", "inspect"):
            if not binding_checked:
                binding_checked = True
                return machine(args)
            if failure == "raise":
                raise TimeoutError("inspection timed out")
            output = {"status": b"", "json": b"{", "shape": b"{}", "empty": b"[{}]"}
            return _WslcResult(1 if failure == "status" else 0, output[failure], b"")
        return machine(args)

    backend, fake = _backend_with(respond)
    with pytest.raises((RuntimeError, ValueError, TimeoutError)):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    assert any(_NAME in call.args for call in fake.matching("container", "remove"))
    assert bool(fake.matching("container", "run")) is (not warm)


def test_instance_id_comes_from_the_engine_on_every_acquire():
    ids = ["a" * 64]
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:2] == ("container", "inspect"):
            return _WslcResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": ids[0],
                            "Config": {"User": "", "Labels": {"maf-sandbox.work-dir.v1": _WORK}},
                        }
                    ]
                ).encode(),
                b"",
            )
        return machine(args)

    backend, _ = _backend_with(respond)
    first = asyncio.run(backend.acquire(_KEY, _SPEC))
    second = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert first is not second
    assert first.instance_id == second.instance_id == "a" * 64
    ids[0] = "b" * 64
    replacement = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert replacement.instance_id == "b" * 64
    assert first.instance_id == "a" * 64


class TestImageCommandProbes:
    @pytest.mark.parametrize("false_status", [0, 1, 126])
    def test_files_in_checks_both_statuses_of_the_pinned_command(self, false_status):
        machine = _machine(running=[_NAME])
        prefix = ("container", "exec", "--user", "0", "-w", "/", f"id-{_NAME}", "/usr/bin/test")

        def respond(args):
            if args[: len(prefix)] == prefix and args[-2] == "-e":
                return _WslcResult(false_status, b"", b"")
            return machine(args)

        backend, fake = _backend_with(respond)
        spec = replace(_SPEC, requires=frozenset({Capability.FILES_IN}))
        if false_status == 1:
            asyncio.run(backend.acquire(_KEY, spec))
        else:
            with pytest.raises(SandboxCapabilityNotSupported, match="/usr/bin/test"):
                asyncio.run(backend.acquire(_KEY, spec))
        probes = [call.args for call in fake.calls if call.read_limit == 1024]
        # The pinned command is checked first, both statuses, before the write utilities and
        # the root setup prerequisites a FILES_IN acquire also needs.
        pinned = [probe for probe in probes if probe[: len(prefix)] == prefix]
        assert len(pinned) == 2
        assert pinned[0] == (*prefix, "-d", "/")
        assert pinned[1][:-1] == (*prefix, "-e")
        assert pinned[1][-1].startswith("/.maf-command-probe-")
        assert probes[:2] == pinned

    @pytest.mark.parametrize(
        "capability,argv,privilege,named",
        [
            (Capability.EXEC, ("sh",), (), "sh"),
            (Capability.FILES_IN, ("/usr/bin/test",), ("--user", "0"), "/usr/bin/test"),
            # The write commands are checked in one guest shell, and the refusal names them.
            (Capability.FILES_IN, ("sh", "-c"), (), "mv"),
        ],
    )
    def test_missing_commands_refuse_acquire_and_can_be_retried(
        self, capability, argv, privilege, named
    ):
        prefix = ("container", "exec", *privilege, "-w", "/", f"id-{_NAME}", *argv)
        backend, fake = _backend_with(
            _machine(
                running=[_NAME],
                overrides={
                    prefix: _WslcResult(127, b"", b"missing executable"),
                },
            )
        )
        spec = replace(_SPEC, requires=frozenset({capability}))
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
        router.ensure_can_serve(spec)
        with pytest.raises(SandboxCapabilityNotSupported, match=named):
            asyncio.run(router.acquire(_KEY, spec))
        assert not fake.matching("container", "remove")
        fake._responder = _machine(running=[_NAME])
        assert asyncio.run(backend.acquire(_KEY, spec)).instance_id == f"id-{_NAME}"

    def test_concurrent_acquires_share_the_successful_command_checks(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))

        async def yielding_engine(*args, **kwargs):
            await asyncio.sleep(0)
            return await fake(*args, **kwargs)

        backend._wslc = yielding_engine

        async def scenario():
            first, second = await asyncio.gather(
                backend.acquire(_KEY, _SPEC),
                backend.acquire(_KEY, _SPEC),
            )
            assert first.instance_id == second.instance_id
            probes = [call for call in fake.calls if call.read_limit == 1024]
            # sh, the pinned test twice, and the guest write utilities.
            assert len(probes) == 4

        asyncio.run(scenario())


def test_a_contended_acquire_lock_does_not_outlive_its_loop():
    """Contend deliberately because an uncontended asyncio lock never binds its loop."""
    backend, _ = _backend_with(_machine(running=[_NAME]))
    loops: list[weakref.ReferenceType[object]] = []

    async def contend():
        async def hold():
            async with backend._acquire_lock(_KEY, _SPEC.kind):
                await asyncio.sleep(0)

        await asyncio.gather(hold(), hold())
        loops.append(weakref.ref(asyncio.get_running_loop()))

    for _ in range(5):
        asyncio.run(contend())
    gc.collect()
    assert not backend._acquire_locks
    assert [reference() for reference in loops] == [None] * len(loops)


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(WslcSandboxBackend(WslcSandboxConfig()), SandboxBackend)

    def test_declares_container_isolation(self):
        """The default `microvm` floor refuses this backend because of this value, by design.

        Superseded by the two-axis floor (#85): was `deployed=True`, now `min_isolation`.
        """
        assert WslcSandboxBackend(WslcSandboxConfig()).isolation == Isolation.CONTAINER

    def test_declares_closed_egress(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).declarations.egress_modes == frozenset(
            {Egress.CLOSED}
        )

    def test_declares_exec_and_files_in_only(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).declarations.capabilities == frozenset(
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


class TestGuestFamilyDeclaration:
    """`os_families` is stated rather than read: `wslc` runs Linux containers and nothing else.

    That constant is what this backend's argv, its POSIX command lines and its guest path
    arithmetic are written against, so the router matches it rather than taking it on trust.
    """

    def test_declares_posix(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).declarations.os_families == frozenset(
            {OsFamily.POSIX}
        )

    def test_no_configuration_moves_it(self):
        """A property of the CLI, so nothing a host sets may reach it — the egress proxy
        image included, which is the one setting that already changes what is declared."""
        proxied = WslcSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
        for config in (WslcSandboxConfig(), proxied):
            assert WslcSandboxBackend(config).declarations.os_families == frozenset(
                {OsFamily.POSIX}
            )


class TestTheRouterMatchesTheDeclaredFamily:
    """The point of the declaration: an axis that refuses something, at attach."""

    @staticmethod
    def _router() -> SandboxRouter:
        return SandboxRouter(
            [WslcSandboxBackend(WslcSandboxConfig())], min_isolation=Isolation.CONTAINER
        )

    def test_a_posix_workload_is_served(self):
        self._router().ensure_can_serve(
            SandboxSpec(
                kind="bicep",
                image="bicep-sandbox:local",
                requires=frozenset({Capability.EXEC}),
                requires_os_family=OsFamily.POSIX,
            )
        )

    def test_a_windows_workload_is_refused(self):
        with pytest.raises(SandboxOsFamilyNotSupported):
            self._router().ensure_can_serve(
                SandboxSpec(
                    kind="bicep",
                    image="bicep-sandbox:local",
                    requires=frozenset({Capability.EXEC}),
                    requires_os_family=OsFamily.WINDOWS,
                )
            )

    def test_a_spec_naming_no_family_is_served(self):
        """A spec that names no family is refused by nothing, which keeps the axis additive."""
        self._router().ensure_can_serve(_SPEC)


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
            SandboxKey(scope="scope-a", thread_id="thread-1", agent_id="devops-engineer"), "bicep"
        )
        assert _container_name(_KEY, "bicep") != _container_name(
            SandboxKey(scope="scope-b", thread_id="thread-1", agent_id="devops-engineer"), "bicep"
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
            "maf-sandbox.work-dir.v1=/maf-sandbox/work",
        ]

    def test_label_values_are_sanitized_at_create(self):
        backend, fake = _backend_with(_machine())
        key = SandboxKey(scope="user-" + "z" * 90, thread_id="thread-1", agent_id="devops")
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
                    return _WslcResult(0, _json_lines(present).encode(), b"")
                return _WslcResult(0, "".join(f"id-{n}\n" for n in present).encode(), b"")
            if args[:2] == ("container", "run"):
                if running_after_the_conflict:
                    present.append(_NAME)
                return _WslcResult(1, b"", b"Error code: ERROR_ALREADY_EXISTS")
            return _machine(running=present)(args)

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


class TestAListingNobodyCanReadIsNotAnEmptyOne:
    """A shape this code does not know has to refuse, not answer "there is nothing there".

    A listing the parser silently read as empty sends `acquire` to create a container that
    already exists, which fails on the name and keeps failing, and it leaves a scope purge
    reporting a clean sweep of a machine it never read.
    """

    #: What `--format json` writes when it is not honoured — the table, holding a real name.
    _WRONG_SHAPE = b"CONTAINER ID  IMAGE     NAMES\nabc123456789  alpine:3  a\n"

    def test_the_parser_refuses_a_payload_that_is_not_json(self):
        from maf_sandbox_wslc._backend import _listed_names, _UnreadableListing

        with pytest.raises(_UnreadableListing):
            _listed_names(self._WRONG_SHAPE.decode())

    def test_the_parser_refuses_a_row_that_carries_no_name(self):
        """A renamed field reaches here as rows without one, which is not a listing of nothing."""
        from maf_sandbox_wslc._backend import _listed_names, _UnreadableListing

        with pytest.raises(_UnreadableListing):
            _listed_names('{"ID":"abc123456789","ContainerName":"a","State":"running"}')

    def test_an_empty_listing_is_still_no_names(self):
        """The CLI prints nothing at all for a listing that matched nothing."""
        from maf_sandbox_wslc._backend import _listed_names

        assert _listed_names("") == []
        assert _listed_names("\n") == []
        assert _listed_names("[]") == []

    def test_acquire_refuses_rather_than_creating_a_second_container(self):
        from maf_sandbox_wslc._backend import _UnreadableListing

        overrides = {("container", "list"): _WslcResult(0, self._WRONG_SHAPE, b"")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))

        with pytest.raises(_UnreadableListing):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("container", "run") == []

    def test_a_failed_adoption_reports_the_conflict_and_why_it_stuck(self):
        """The conflict is what failed; the listing is why the fallback could not clear it."""
        overrides = {
            ("container", "list"): _WslcResult(0, self._WRONG_SHAPE, b""),
            ("container", "run"): _WslcResult(1, b"", b"Error code: ERROR_ALREADY_EXISTS"),
        }
        backend, _ = _backend_with(_machine(overrides=overrides))
        backend._is_listed = _only_on_create(backend, self._WRONG_SHAPE)  # noqa: SLF001

        with pytest.raises(RuntimeError) as raised:
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert "ERROR_ALREADY_EXISTS" in str(raised.value)
        assert "could not read the container listing" in str(raised.value)

    def test_a_scope_purge_says_it_may_be_partial(self):
        overrides = {("container", "list"): _WslcResult(0, self._WRONG_SHAPE, b"")}
        backend, fake = _backend_with(_machine(stopped=["a", "b"], overrides=overrides))

        purge = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert purge.undisposed is not None
        assert purge.undisposed.code == "unlisted"
        assert fake.matching("container", "remove") == []


def _only_on_create(backend, payload: bytes):
    """`_is_listed` that answers "absent" until a create has run, then cannot read the listing.

    Acquire has to reach the create branch for the conflict to happen at all, so the listing
    breaks where `_adopt` reads it rather than where `acquire` does.
    """
    from maf_sandbox_wslc._backend import _UnreadableListing

    created = False
    original = backend._create_workload  # noqa: SLF001

    async def create(*args, **kwargs):
        nonlocal created
        created = True
        return await original(*args, **kwargs)

    backend._create_workload = create  # noqa: SLF001

    async def is_listed(name: str, *, all_states: bool) -> bool:
        if created:
            raise _UnreadableListing(f"could not read the container listing: {payload!r}")
        return False

    return is_listed


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


class TestExecArgv:
    def test_a_sequence_reaches_the_container_verbatim_with_no_shell(self):
        """`wslc exec` takes argv natively, so nothing needs quoting and nothing may be
        re-interpreted: an element containing `;` stays one argument."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        argv = ["echo", "a; rm -rf /", "$(id)"]
        asyncio.run(sandbox.exec(argv, working_directory="/maf-sandbox/work", timeout=5))

        args = fake.only("container", "exec").args
        assert args == ("container", "exec", "-w", "/maf-sandbox/work", _NAME, *argv)
        assert "sh" not in args

    def test_a_string_is_run_by_a_shell(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.exec(["true"], working_directory="/w", timeout=12.5))

        assert fake.only("container", "exec").timeout == 12.5


class TestExecResult:
    def test_both_raw_streams_survive_the_adapter(self):
        raw = bytes(range(256))
        overrides = {("container", "exec", "-w", "/w"): _WslcResult(7, raw, raw[::-1])}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        result = asyncio.run(sandbox.exec(["x"], working_directory="/w", timeout=5))
        assert result.stdout_bytes == raw
        assert result.stderr_bytes == raw[::-1]

    def test_stdout_stderr_and_exit_code_are_mapped_verbatim(self):
        overrides = {("container", "exec", "-w", "/w"): _WslcResult(7, b"out\n", b"err\n")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
            if args[:4] == ("container", "exec", "-w", "/w"):
                raise TimeoutError("wslc exec did not answer")
            return base(args)

        return _backend_with(respond)

    def test_a_timed_out_exec_removes_the_container(self):
        """By instance ID: a name is reusable, so a late discard could reach a replacement."""
        backend, fake = self._timing_out()
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["sleep", "600"], working_directory="/w", timeout=1))

        assert fake.only("container", "remove").args == ("container", "remove", "-f", f"id-{_NAME}")

    def test_the_timeout_still_reaches_the_caller(self):
        """The workload reports a hang as a diagnostic; swallowing it would report success."""
        backend, _ = self._timing_out()
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec("sleep 600", working_directory="/w", timeout=1))

    def test_a_removal_that_also_fails_does_not_mask_the_timeout(self):
        base = _machine(running=[_NAME])

        def respond(args: tuple[str, ...]) -> _WslcResult:
            if args[:4] == ("container", "exec", "-w", "/w") or args[:2] == ("container", "remove"):
                raise TimeoutError("wslc is not answering at all")
            return base(args)

        backend, _ = _backend_with(respond)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["sleep", "600"], working_directory="/w", timeout=1))


# ---------------------------------------------------------------------------
# reclaim
# ---------------------------------------------------------------------------


class TestReclaim:
    @pytest.mark.parametrize("directory", ["/work/call", "/tmp/linked/call", "/", "relative"])
    def test_reclaim_refuses_without_running_a_guest_command(self, directory):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        seen = len(fake.calls)
        with pytest.raises(NotImplementedError, match="RECLAIM.*Dispose the sandbox"):
            asyncio.run(sandbox.reclaim(directory, working_directory=_WORK, timeout=30))
        assert len(fake.calls) == seen

    @pytest.mark.parametrize("confined", [False, True])
    @pytest.mark.parametrize("floor", list(Cleanup))
    def test_every_workload_resolves_to_disposal(self, confined, floor):
        backend, _ = _backend_with()
        router = SandboxRouter(
            backends=[backend], min_isolation=Isolation.CONTAINER, min_cleanup=floor
        )
        spec = replace(_SPEC, confined_to_guest_call_path=confined)
        assert Capability.RECLAIM not in backend.declarations.capabilities
        assert Capability.SNAPSHOT not in backend.declarations.capabilities
        assert router.effective_cleanup(spec) is Cleanup.DISPOSE


class TestGuestPrincipal:
    @pytest.mark.parametrize(
        ("result", "principal"),
        [
            (_WslcResult(0, b"0\n", b""), "root"),
            (_WslcResult(0, b"1000\n", b""), "unprivileged"),
            (_WslcResult(0, b"", b""), "unknown"),
            (_WslcResult(1, b"0", b"failed"), "unknown"),
            (_WslcResult(0, b"-1", b""), "unknown"),
            (_WslcResult(0, b"0\n1000", b""), "unknown"),
            (_WslcResult(0, b"root", b""), "unknown"),
        ],
    )
    def test_reported_principal_names_the_refusal_and_cleanup(self, result, principal, caplog):
        probe = ("container", "exec", "-w", "/", _NAME, "id", "-u")
        backend, fake = _backend_with(_machine(running=[_NAME], overrides={probe: result}))
        with caplog.at_level(logging.INFO):
            sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        assert sandbox.guest_principal == principal
        assert f"guest_principal={principal}" in caplog.text
        assert "cleanup=dispose" in caplog.text
        assert len(fake.matching(*probe)) == 1
        assert fake.matching(*probe)[0].read_limit == 64
        seen = len(fake.calls)
        with pytest.raises(NotImplementedError, match=f"Guest principal: {principal}"):
            asyncio.run(sandbox.remove("x", working_directory=_WORK))
        with pytest.raises(NotImplementedError, match=f"Guest principal: {principal}"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/call", working_directory=_WORK, timeout=30))
        assert len(fake.calls) == seen

    def test_a_failed_probe_is_retried_and_never_cached_by_name(self):
        """An answer this backend cannot use is an unresolved identity, not a dirty container."""
        answers = iter(
            [
                _WslcResult(1, b"", b"id: cannot find name for user ID"),
                _WslcResult(0, b"0", b""),
                _WslcResult(0, b"1000", b""),
            ]
        )
        machine = _machine(running=[_NAME])

        def respond(args):
            return next(answers) if args[-2:] == ("id", "-u") else machine(args)

        backend, fake = _backend_with(respond)
        principals = [asyncio.run(backend.acquire(_KEY, _SPEC)).guest_principal for _ in range(3)]
        assert principals == ["unknown", "root", "unprivileged"]
        assert not fake.matching("container", "remove"), "the container answered and was kept"

    @pytest.mark.parametrize("ending", ["times out", "fills the read cap"])
    def test_an_identity_probe_that_does_not_end_discards_the_container(self, ending):
        """`id` runs in the guest, and killing the host process does not reach it.

        Acquire is about to hand this container to a caller and a warm acquire would reuse
        it, so an ask whose end is unknown takes the container with it.
        """
        machine = _machine(running=[_NAME])

        def respond(args):
            if args[-2:] == ("id", "-u"):
                if ending == "times out":
                    raise TimeoutError("the identity probe did not answer")
                return _WslcResult(0, b"9" * 64, b"")
            return machine(args)

        backend, fake = _backend_with(respond)
        with pytest.raises((TimeoutError, RuntimeError)):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("container", "remove")[-1].args[-1] == f"id-{_NAME}"
        assert not backend._undiscarded, "the removal succeeded, so nothing stays quarantined"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    @pytest.mark.parametrize("work", ["workspace", "./workspace", "/workspace", "//workspace"])
    def test_work_dir_spellings_reach_one_target(self, work):
        spec = replace(_METHOD_SPEC, work_dir=work if work.startswith("/") else "/")
        name = _container_name(_KEY, spec.kind)
        backend, fake = _backend_with(_machine(running=[name], work_dir=str(spec.work_dir)))
        sandbox = asyncio.run(backend.acquire(_KEY, spec))
        asyncio.run(sandbox.write_file("call-a1/nested/input", b"data", working_directory=work))
        target, parent, staged, size = _operands(_only_write(fake))
        assert target == "/workspace/call-a1/nested/input"
        assert parent == "/workspace/call-a1/nested"
        assert re.fullmatch(r"/workspace/call-a1/nested/\.maf-[0-9a-f]{32}\.part", staged)
        assert size == "4"

    @pytest.mark.parametrize("user", ["10001:20001", "", "worker"])
    def test_the_write_runs_as_the_image_user_with_the_content_on_stdin(self, user):
        """No ``--user``: the principal the guest program runs as places the file.

        That is what bounds a parent swapped after the check, and why a write needs no
        resolved identity: nothing is stamped.
        """
        labels = {"maf-sandbox.work-dir.v1": _WORK}
        inspected = {"Id": "i", "Config": {"User": user, "Labels": labels}}
        overrides = {
            ("container", "inspect"): _WslcResult(0, json.dumps([inspected]).encode(), b""),
            ("container", "exec", "-w", "/", _NAME, "id"): _WslcResult(1, b"", b"no id"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.write_file("nested/input", b"data", working_directory=_WORK))
        call = _only_write(fake)
        assert call.args[: call.args.index(_WRITE_AS_THE_GUEST) + 2] == (
            "container",
            "exec",
            "-i",
            "-w",
            "/",
            _NAME,
            "sh",
            "-c",
            _WRITE_AS_THE_GUEST,
            "sh",
        )
        assert call.stdin == b"data"
        assert not fake.matching("container", "cp", "-")

    #: Inspection payloads this backend cannot read at all. They fail acquire, but on the
    #: engine's shape rather than on the image's user, so they carry no typed promise.
    _UNREADABLE_INSPECTIONS = [
        _WslcResult(1, b"", b"unavailable"),
        _WslcResult(0, b"not json", b""),
        _WslcResult(0, b"[]", b""),
        _WslcResult(0, b"{}", b""),
    ]

    #: Inspection payloads this backend reads fine and that leave the image user unresolved:
    #: absent, a name whose `id` does not answer, and an out-of-range uid.
    _UNRESOLVED_IDENTITIES = [
        _WslcResult(
            0,
            b'[{"Id":"instance","Config":{"Labels":{"maf-sandbox.work-dir.v1":"/maf-sandbox/work"},"User":null}}]',
            b"",
        ),
        _WslcResult(
            0,
            b'[{"Id":"instance","Config":{"Labels":{"maf-sandbox.work-dir.v1":"/maf-sandbox/work"},"User":"worker"}}]',
            b"",
        ),
        _WslcResult(
            0,
            b'[{"Id":"instance","Config":{"Labels":{"maf-sandbox.work-dir.v1":"/maf-sandbox/work"},"User":"4294967295:0"}}]',
            b"",
        ),
    ]

    @pytest.mark.parametrize("inspection", _UNREADABLE_INSPECTIONS)
    def test_an_unreadable_inspection_fails_acquire(self, inspection):
        """Not a capability verdict: the engine's answer was unusable, not the image's user."""
        backend, fake = _backend_with(
            _machine(running=[_NAME], overrides={("container", "inspect"): inspection})
        )
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert not _writes(fake) and not _creations(fake)

    @pytest.mark.parametrize("inspection", _UNRESOLVED_IDENTITIES)
    def test_unresolved_identity_refuses_a_base_it_would_have_to_create(self, inspection):
        """Creating a base needs an owner, so an unresolved user stops it before it starts.

        The type is the contract here, not just the failure: a caller tells "this image
        cannot serve that" from "the engine broke" by the exception it gets. Accepting a
        bare ``RuntimeError`` would let that guard regress unnoticed.
        """
        backend, fake = _backend_with(
            _machine(running=[_NAME], overrides={("container", "inspect"): inspection})
        )
        with pytest.raises(SandboxCapabilityNotSupported, match="image user it would belong to"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert not _writes(fake) and not _creations(fake)
        assert not fake.matching("container", "cp", "-")

    @pytest.mark.parametrize("work", [_WORK, "/etc"])
    def test_a_base_that_was_already_there_keeps_its_owner(self, work):
        """``acquire`` preserves the ownership it finds, so an existing base is never chowned.

        Chowning one hands the image's user a directory the host never offered — with
        ``work_dir="/etc"`` that is the guest owning ``/etc`` and every entry it can unlink.
        Ownership goes only to a base this backend created.
        """
        ancestors = ("/", "/maf-sandbox", _WORK) if work == _WORK else ("/", "/etc")
        overrides = {
            ("container", "cp", f"{_NAME}:{guest}"): _cp_is_a_directory() for guest in ancestors
        }
        overrides[("container", "inspect")] = _WslcResult(
            0,
            json.dumps(
                [
                    {
                        "Id": "i",
                        "Config": {
                            "User": "10001:20001",
                            "Labels": {"maf-sandbox.work-dir.v1": work},
                        },
                    }
                ]
            ).encode(),
            b"",
        )
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides, work_dir=work))
        asyncio.run(backend.acquire(_KEY, replace(_SPEC, work_dir=work)))
        assert not _creations(fake)

    def test_a_base_this_acquire_created_goes_to_the_guest(self):
        overrides = {
            ("container", "cp", f"{_NAME}:{guest}"): _cp_is_a_directory()
            for guest in ("/", "/maf-sandbox")
        }
        overrides[("container", "inspect")] = _WslcResult(
            0,
            json.dumps(
                [
                    {
                        "Id": "i",
                        "Config": {
                            "User": "10001:20001",
                            "Labels": {"maf-sandbox.work-dir.v1": _WORK},
                        },
                    }
                ]
            ).encode(),
            b"",
        )
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        (created,) = _creations(fake)
        assert created.args[created.args.index(_CREATE_DIRECTORIES) + 1 :] == (
            "sh",
            "10001:20001",
            "/",
            "/maf-sandbox",
            "--",
            _WORK,
        )

    def test_existing_directories_are_held_rather_than_created(self):
        """Only the missing suffix is created; an existing parent is where the shell starts."""
        overrides = {
            ("container", "cp", f"{_NAME}:/maf-sandbox"): _cp_is_a_directory(),
            ("container", "inspect"): _WslcResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": "i",
                            "Config": {
                                "User": "10001:20001",
                                "Labels": {"maf-sandbox.work-dir.v1": _WORK},
                            },
                        }
                    ]
                ).encode(),
                b"",
            ),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        (created,) = _creations(fake)
        start = created.args.index(_CREATE_DIRECTORIES) + 3
        assert created.args[start:] == ("/", "/maf-sandbox", "--", _WORK)

    @pytest.mark.parametrize("gid", [b"", b"-1", b"staff", b"20001\n0", b"4294967295"])
    def test_a_named_user_with_no_valid_group_cannot_write(self, gid):
        overrides = {
            ("container", "inspect"): _WslcResult(
                0,
                b'[{"Id":"instance","Config":{"Labels":{"maf-sandbox.work-dir.v1":"/maf-sandbox/work"},"User":"worker"}}]',
                b"",
            ),
            ("container", "exec", "-w", "/", _NAME, "id", "-u"): _WslcResult(0, b"10001", b""),
            ("container", "exec", "-w", "/", _NAME, "id", "-g"): _WslcResult(0, gid, b""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        with pytest.raises(SandboxCapabilityNotSupported, match="image user it would belong to"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert not _writes(fake) and not _creations(fake)

    def test_each_acquire_resolves_the_base_owner_again(self):
        # What the image reports, changed between acquires rather than counted out per
        # inspect: an acquire does not promise how many times it asks the engine.
        user: str | None = None
        machine = _machine(running=[_NAME])

        def respond(args):
            if args[:2] != ("container", "inspect"):
                return machine(args)
            config: dict[str, object] = {"Labels": {"maf-sandbox.work-dir.v1": "/maf-sandbox/work"}}
            if user is not None:
                config["User"] = user
            return _WslcResult(0, json.dumps([{"Id": "instance", "Config": config}]).encode(), b"")

        backend, fake = _backend_with(respond)
        with pytest.raises(SandboxCapabilityNotSupported, match="image user it would belong to"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        for expected in ("10001:20001", "10002:20002"):
            user = expected
            asyncio.run(backend.acquire(_KEY, _SPEC))
            created = _creations(fake)[-1]
            assert created.args[created.args.index(_CREATE_DIRECTORIES) + 2] == expected

    def test_working_at_root_writes_beneath_it(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.write_file("nested/input", b"data", working_directory="/"))
        assert _operands(_only_write(fake))[:2] == ("/nested/input", "/nested")

    def _sent(self, path: str, content: str | bytes) -> _Recorded:
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.write_file(path, content, working_directory=_WORK))
        return _only_write(fake)

    def test_an_absolute_path_inside_the_working_directory_is_its_own_target(self):
        target, parent, staged, _ = _operands(self._sent("/maf-sandbox/work/r1/main.bicep", "x"))
        assert (target, parent) == ("/maf-sandbox/work/r1/main.bicep", "/maf-sandbox/work/r1")
        assert posixpath.dirname(staged) == parent

    def test_a_relative_path_is_left_alone(self):
        target = _operands(self._sent("maf-sandbox/work/main.bicep", "x"))[0]
        assert target == "/maf-sandbox/work/maf-sandbox/work/main.bicep"

    def test_the_content_round_trips_as_utf8(self):
        call = self._sent("/maf-sandbox/work/main.bicep", "param naïve string\n")
        assert call.stdin == "param naïve string\n".encode()
        assert _operands(call)[3] == str(len("param naïve string\n".encode()))

    def test_bytes_are_written_as_given(self):
        """The protocol's ``write_file`` takes ``str | bytes`` — an in-door carrying a PNG or a
        spreadsheet needs bytes, and they must reach the guest unencoded. Raising
        ``AttributeError`` on ``bytes.encode`` here was the load-bearing half of #370."""
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        call = self._sent("/maf-sandbox/work/diagram.png", payload)
        assert call.stdin == payload
        assert _operands(call)[3] == str(len(payload))

    def test_each_write_stages_beside_its_target_under_a_new_name(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        for _ in range(2):
            asyncio.run(sandbox.write_file("input", b"x", working_directory=_WORK))
        first, second = (_operands(call)[2] for call in _writes(fake))
        assert first != second
        assert posixpath.dirname(first) == posixpath.dirname(second) == _WORK

    @pytest.mark.parametrize(
        ("stderr", "error"),
        [
            (b"mkdir: cannot create directory '/etc/x': Permission denied", PermissionError),
            (b"sh: can't create /x/.maf-a.part: Permission denied", PermissionError),
            (b"Is a directory", IsADirectoryError),
            (b"mkdir: cannot create directory '/x/f': Not a directory", NotADirectoryError),
            (b"mkdir: cannot create directory '/x/f': File exists", NotADirectoryError),
            (b"cat: /x/.maf-a.part: No such file or directory", FileNotFoundError),
            (b"the content was cut short", RuntimeError),
            (b"WSLC_E_CONTAINER_NOT_FOUND", RuntimeError),
        ],
    )
    def test_a_failed_write_raises_what_the_guest_said(self, stderr, error):
        """A write that silently did nothing would surface as a compiler error about a file
        the workload believes it just wrote."""
        overrides = {("container", "exec", "-i"): _WslcResult(1, b"", stderr)}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        with pytest.raises(error, match="could not write /maf-sandbox/work/main.bicep") as raised:
            asyncio.run(
                sandbox.write_file("/maf-sandbox/work/main.bicep", "x", working_directory=_WORK)
            )
        assert type(raised.value) is error
        assert stderr.decode() in str(raised.value)

    def test_a_refused_path_never_reaches_the_guest(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        with pytest.raises(ValueError):
            asyncio.run(sandbox.write_file("../escape", "x", working_directory=_WORK))
        assert _writes(fake) == []

    #: A filesystem the check can actually get through: every directory above the work dir
    #: answers as one, which is what the engine's own refusal to copy a directory looks like.
    #: Without it the first component reads as absent and the check ends before it has begun.
    _REAL_DIRECTORIES = {
        ("container", "cp", f"{_NAME}:{parent}"): _cp_is_a_directory()
        for parent in ("/", "/maf-sandbox", _WORK)
    }

    @pytest.mark.parametrize(
        ("claim", "refusal"),
        [("-L", ValueError), ("-d", NotADirectoryError), ("-e", NotADirectoryError)],
    )
    def test_no_answer_about_a_linked_parent_carries_the_write_through_it(self, claim, refusal):
        """A link above the leaf is refused whatever the container says it is.

        `test` runs inside that container, so the workload picks the answer. It picks which
        refusal the caller sees and nothing else: the engine refuses to copy a directory, so a
        component it accepted is not one, and every claim here ends in a refusal with no write
        command reaching the guest.
        """
        overrides = {
            ("container", "cp", f"{_NAME}:{_WORK}/ld"): _WslcResult(0, b"", b""),
            **self._REAL_DIRECTORIES,
            (*_PROBE, claim): _WslcResult(0, b"", b""),
            _PROBE: _WslcResult(1, b"", b""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        with pytest.raises(refusal):
            asyncio.run(sandbox.write_file("ld/landed", b"x", working_directory=_WORK))
        assert _writes(fake) == []

    def test_an_existing_file_at_the_leaf_is_written_over(self):
        """The leaf is the one component the guest's word is taken on, and it is bounded.

        A link here is refused; anything else is written over. A guest lying the other way —
        hiding a link at its own leaf — gets nothing its own user could not write, because
        the write runs as that user.
        """
        overrides = {
            ("container", "cp", f"{_NAME}:{_WORK}/main.bicep"): _WslcResult(0, b"", b""),
            **self._REAL_DIRECTORIES,
            (*_PROBE, "-f"): _WslcResult(0, b"", b""),
            _PROBE: _WslcResult(1, b"", b""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.write_file("main.bicep", b"second", working_directory=_WORK))
        assert _operands(_only_write(fake))[0] == f"{_WORK}/main.bicep"


#: The offline suite runs on Linux in CI; Windows has no POSIX shell to run these in.
if sys.platform == "win32":
    _POSIX_SH, _OWNER, _AS_ROOT = None, "", False
else:
    _POSIX_SH, _OWNER, _AS_ROOT = (
        shutil.which("sh"),
        f"{os.getuid()}:{os.getgid()}",
        os.geteuid() == 0,
    )


@pytest.mark.skipif(_POSIX_SH is None, reason="needs a POSIX sh")
class TestTheFileCommandsInARealShell:
    """The file commands run as written, in the host's own ``sh``.

    The live suite runs them in a container. Here, links planted before the command starts
    stand in for a swap after the check: the command must refuse what it finds. Setup's walk
    starts at ``tmp_path`` rather than ``/``, and "root's" means the test's own user.
    """

    @staticmethod
    def _write(
        target: Path, content: bytes, *, size: int | None = None, env: dict[str, str] | None = None
    ):
        parent = str(target.parent)
        return subprocess.run(
            ["sh", "-c", _WRITE_AS_THE_GUEST, "sh", str(target), parent, f"{parent}/.maf-0.part"]
            + [str(len(content) if size is None else size)],
            input=content,
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )

    @staticmethod
    def _create(
        start: Path,
        existing: Sequence[str],
        missing: Sequence[str],
        env: dict[str, str] | None = None,
        script: str = _CREATE_DIRECTORIES,
    ):
        """Run setup from ``start``, naming each directory relative to it."""
        return subprocess.run(
            ["sh", "-c", script, "sh", _OWNER, str(start)]
            + [str(start / name) for name in existing]
            + ["--"]
            + [str(start / name) for name in missing],
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )

    @staticmethod
    def _create_as_the_guest(base: Path, env: dict[str, str] | None = None):
        return subprocess.run(
            [_POSIX_SH or "sh", "-c", _CREATE_AS_THE_GUEST, "sh", str(base)],
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )

    @staticmethod
    def _start(tmp_path: Path) -> Path:
        """A walk's first directory: this user's, and writable by nobody else."""
        start = tmp_path.resolve()
        start.chmod(0o755)
        return start

    def test_a_write_creates_its_parents_and_lands_whole(self, tmp_path):
        target = tmp_path / "a" / "b" / "input"
        done = self._write(target, b"data")
        assert done.returncode == 0, done.stderr
        assert target.read_bytes() == b"data"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o755
        assert [path.name for path in target.parent.iterdir()] == ["input"]

    def test_a_write_replaces_a_file_and_keeps_its_parents_mode(self, tmp_path):
        tmp_path.chmod(0o700)
        target = tmp_path / "input"
        target.write_bytes(b"before")
        done = self._write(target, b"after")
        assert done.returncode == 0, done.stderr
        assert target.read_bytes() == b"after"
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700

    def test_content_cut_short_never_lands(self, tmp_path):
        target = tmp_path / "input"
        target.write_bytes(b"before")
        done = self._write(target, b"part", size=10)
        assert done.returncode == 1
        assert done.stderr.decode().strip() == "the content was cut short"
        assert target.read_bytes() == b"before"
        assert [path.name for path in tmp_path.iterdir()] == ["input"]

    def test_a_directory_at_the_target_is_refused_and_left_empty(self, tmp_path):
        target = tmp_path / "input"
        target.mkdir()
        done = self._write(target, b"x")
        assert shell_refusal(done.stderr.decode()) is FileRefusal.IS_DIRECTORY
        assert list(target.iterdir()) == []
        assert [path.name for path in tmp_path.iterdir()] == ["input"]

    @pytest.mark.skipif(_AS_ROOT, reason="root writes anywhere")
    def test_a_parent_the_user_cannot_write_is_refused(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o555)
        try:
            done = self._write(locked / "input", b"x")
            assert shell_refusal(done.stderr.decode()) is FileRefusal.PERMISSION_DENIED
            assert list(locked.iterdir()) == []
        finally:
            locked.chmod(0o755)

    def test_a_leaf_turned_into_a_directory_at_the_rename_is_refused(self, tmp_path):
        """``mv`` treats a destination directory as a container and reports success.

        The stand-in for ``mv`` makes the leaf a directory in the one place it matters —
        after the last check and before the rename — so without the check that follows it,
        the write would report success with the content at ``<target>/<sibling>``.
        """
        real = shutil.which("mv")
        assert real, "the real mv has to be somewhere for the wrapper to call"
        binaries = tmp_path / "bin"
        binaries.mkdir()
        wrapper = binaries / "mv"
        # `mv -f -- <staged> <target>`, so the target is the fourth argument.
        wrapper.write_text("\n".join(["#!/bin/sh", 'mkdir -p "$4"', f'exec {real} "$@"', ""]))
        wrapper.chmod(0o755)
        target = tmp_path / "target"
        done = self._write(
            target, b"payload", env={"PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"}
        )
        assert done.returncode != 0
        assert shell_refusal(done.stderr.decode()) is FileRefusal.IS_DIRECTORY
        # The misplaced sibling is taken back rather than left inside the directory.
        assert target.is_dir() and list(target.iterdir()) == []

    def test_setup_creates_each_missing_directory(self, tmp_path):
        start = self._start(tmp_path)
        done = self._create(start, [], ["a", "a/b"])
        assert done.returncode == 0, done.stderr
        assert done.stdout == b""
        assert (start / "a" / "b").is_dir()
        assert stat.S_IMODE((start / "a").stat().st_mode) == 0o755

    def test_setup_ignores_an_inherited_cdpath(self, tmp_path):
        """A relative ``cd`` must reach the directory just made, not a same-named decoy.

        ``CDPATH`` is cleared in the script, so an inherited one cannot divert the loop's
        ``cd`` into a directory of the same name that happens to sit under a ``CDPATH`` entry.
        """
        start = self._start(tmp_path)
        decoy = start / "decoy"
        (decoy / "a").mkdir(parents=True)
        done = self._create(start, [], ["a", "a/b"], env={"CDPATH": str(decoy)})
        assert done.returncode == 0, done.stderr
        assert (start / "a" / "b").is_dir()
        assert not (decoy / "a" / "b").exists()

    @pytest.mark.parametrize("mode", [0o775, 0o757, 0o1777])
    @pytest.mark.parametrize("where", ["the start", "above the parent", "the parent"])
    def test_setup_leaves_a_directory_others_can_write_to_the_guest(self, tmp_path, mode, where):
        """Root does not act inside a directory whose entries another user can replace.

        Group and other write bits alike, and sticky too: a sticky directory still lets its
        writers rename an entry they own into place.
        """
        start = self._start(tmp_path)
        (start / "outer" / "inner").mkdir(parents=True)
        writable = {
            "the start": start,
            "above the parent": start / "outer",
            "the parent": start / "outer" / "inner",
        }[where]
        writable.chmod(mode)
        try:
            done = self._create(start, ["outer", "outer/inner"], ["base"])
        finally:
            writable.chmod(0o755)
        assert done.returncode == 0, done.stderr
        assert done.stdout.decode().strip() == _LEFT_TO_THE_GUEST
        assert not (start / "outer" / "inner" / "base").exists()

    @pytest.mark.skipif(_AS_ROOT, reason="root owns / and the walk would proceed")
    def test_setup_leaves_a_directory_another_user_owns_to_the_guest(self, tmp_path):
        """The walk starts at ``/``, which is not this user's, so nothing is created."""
        done = subprocess.run(
            ["sh", "-c", _CREATE_DIRECTORIES, "sh", _OWNER, "/", "--", str(tmp_path / "base")],
            capture_output=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.decode().strip() == _LEFT_TO_THE_GUEST
        assert not (tmp_path / "base").exists()

    @pytest.mark.parametrize("answer", ["exit 1", "echo total 0"])
    def test_setup_leaves_to_the_guest_a_mode_ls_did_not_report(self, tmp_path, answer):
        """A mode ``ls`` failed to report, or reported as something else, is not a safe one."""
        binaries = tmp_path / "bin"
        binaries.mkdir()
        (binaries / "ls").write_text(f"#!/bin/sh\n{answer}\n")
        (binaries / "ls").chmod(0o755)
        script = _CREATE_DIRECTORIES.replace(
            f"PATH={SETUP_PATH}", f"PATH={binaries}{os.pathsep}{SETUP_PATH}"
        )
        assert script != _CREATE_DIRECTORIES
        (tmp_path / "start").mkdir()
        start = self._start(tmp_path / "start")
        done = self._create(start, [], ["base"], script=script)
        assert done.returncode == 0, done.stderr
        assert done.stdout.decode().strip() == _LEFT_TO_THE_GUEST
        assert not (start / "base").exists()

    def test_setup_refuses_a_parent_swapped_for_a_link(self, tmp_path):
        start = self._start(tmp_path)
        protected = start / "protected"
        protected.mkdir()
        (start / "parent").symlink_to(protected)
        done = self._create(start, ["parent"], ["parent/child"])
        assert done.returncode == 1
        assert b"does not resolve to itself any more" in done.stderr
        assert list(protected.iterdir()) == []

    def test_setup_refuses_a_link_above_the_parent(self, tmp_path):
        start = self._start(tmp_path)
        (start / "real" / "parent").mkdir(parents=True)
        (start / "via").symlink_to(start / "real")
        done = self._create(start, ["via", "via/parent"], ["via/parent/child"])
        assert done.returncode == 1
        assert list((start / "real" / "parent").iterdir()) == []

    def test_setup_refuses_a_link_planted_where_a_directory_was_missing(self, tmp_path):
        start = self._start(tmp_path)
        protected = start / "protected"
        protected.mkdir()
        (start / "child").symlink_to(protected)
        done = self._create(start, [], ["child", "child/base"])
        assert done.returncode == 1
        assert (start / "child").is_symlink()
        assert list(protected.iterdir()) == []

    def test_the_guest_creates_every_missing_directory(self, tmp_path):
        base = tmp_path / "a" / "b"
        done = self._create_as_the_guest(base)
        assert done.returncode == 0, done.stderr
        assert base.is_dir()
        assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o755

    def test_the_guest_command_names_a_missing_mkdir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        done = self._create_as_the_guest(tmp_path / "base", env={"PATH": str(empty)})
        assert done.returncode == 127
        assert done.stderr.decode().strip() == "maf-setup-missing mkdir"
        assert not (tmp_path / "base").exists()

    @pytest.mark.skipif(_AS_ROOT, reason="root writes anywhere")
    def test_the_guest_cannot_create_inside_a_directory_it_cannot_write(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o555)
        try:
            done = self._create_as_the_guest(locked / "base")
            assert shell_refusal(done.stderr.decode()) is FileRefusal.PERMISSION_DENIED
            assert list(locked.iterdir()) == []
        finally:
            locked.chmod(0o755)


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

    def test_run_code_raises_notimplementederror(self):
        """This backend declares no RUN_CODE, and the reason is not that a guest lacks an
        interpreter — it is that the backend is handed an image reference it does not parse,
        so it cannot know which runtime is inside. Declaring it would be a claim about
        someone else's artefact."""
        sandbox = self._sandbox()
        with pytest.raises(NotImplementedError, match="RUN_CODE"):
            asyncio.run(sandbox.run_code("print(1)", timeout=5.0))


class TestStatHostStorage:
    @pytest.mark.parametrize(
        "outcome", ["success", "engine-error", "probe-error", "timeout", "cancel"]
    )
    def test_copied_bytes_are_removed_on_every_exit(self, tmp_path, monkeypatch, outcome):
        monkeypatch.chdir(tmp_path)
        sentinel = tmp_path / "-"
        sentinel.write_bytes(b"host content")
        copies = tmp_path / "copies"
        copies.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(copies))
        destinations = []

        async def scenario():
            copied = asyncio.Event()

            async def run(*args, **kwargs):
                if args[:2] == ("container", "cp"):
                    destination = Path(args[-1])
                    destinations.append(destination)
                    destination.write_bytes(b"guest bytes" * 65536)
                    copied.set()
                    if outcome == "timeout":
                        await asyncio.wait_for(asyncio.Event().wait(), timeout=0.01)
                    if outcome == "cancel":
                        await asyncio.Event().wait()
                    if outcome == "engine-error":
                        return _WslcResult(1, b"", b"copy failed")
                    return _WslcResult(0, b"", b"")
                assert not list(copies.iterdir())
                if outcome == "probe-error":
                    raise OSError("probe failed")
                return _WslcResult(0 if args[-2] == "-f" else 1, b"", b"")

            sandbox = _WslcSandbox(run, _NAME, 30, instance_id="instance")
            task = asyncio.create_task(sandbox._stat_guest("/w/file", "file"))
            await copied.wait()
            if outcome == "cancel":
                task.cancel()
            if outcome == "success":
                entry = await task
                assert entry is not None and entry.kind is EntryKind.FILE
            else:
                error = {
                    "engine-error": RuntimeError,
                    "probe-error": OSError,
                    "timeout": TimeoutError,
                    "cancel": asyncio.CancelledError,
                }[outcome]
                with pytest.raises(error):
                    await task

        asyncio.run(asyncio.wait_for(scenario(), timeout=3))
        assert sentinel.read_bytes() == b"host content"
        assert len(destinations) == 1 and destinations[0].is_absolute()
        assert destinations[0].parent.parent == copies
        assert not list(copies.iterdir())

    def test_concurrent_stats_have_independent_copy_targets(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        destinations = []

        async def scenario():
            ready = asyncio.Event()

            async def run(*args, **kwargs):
                if args[:2] == ("container", "cp"):
                    destination = Path(args[-1])
                    destinations.append(destination)
                    destination.write_bytes(args[2].encode())
                    if len(destinations) == 2:
                        ready.set()
                    await ready.wait()
                    assert destination.read_bytes() == args[2].encode()
                    return _WslcResult(0, b"", b"")
                return _WslcResult(0 if args[-2] == "-f" else 1, b"", b"")

            sandbox = _WslcSandbox(run, _NAME, 30, instance_id="instance")
            await asyncio.gather(sandbox._stat_guest("/w/a", "a"), sandbox._stat_guest("/w/b", "b"))

        asyncio.run(asyncio.wait_for(scenario(), timeout=3))
        assert len(set(destinations)) == 2
        assert not list(tmp_path.iterdir())


class TestStatGuest:
    """The engine settles absence and directories; the guest splits accepted sources."""

    def _sandbox(self, overrides: dict | None = None):
        return self._sandbox_and_fake(overrides)[0]

    def _sandbox_and_fake(self, overrides: dict | None = None):
        """The sandbox and the fake behind it, for a case asserting the argv it was handed."""
        if overrides is None:
            overrides = {("container", "cp"): _WslcResult(0, b"", b"")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _METHOD_SPEC)), fake

    def test_an_unrecognised_failure_raises_with_the_engines_message(self):
        """A `cp` that failed with nothing to classify reports what the CLI said. The error
        names the path so the caller can tell which component tripped."""
        overrides = {("container", "cp"): _WslcResult(1, b"", b"container is stopped")}
        sandbox = self._sandbox(overrides=overrides)
        with pytest.raises(RuntimeError, match="container is stopped"):
            asyncio.run(sandbox._stat_guest("/w/main.bicep", "main.bicep"))

    @pytest.mark.parametrize("stdout", [b"diagnostic", tarfile.TarInfo("sub/").tobuf()])
    def test_stdout_cannot_make_a_failed_copy_a_success(self, stdout):
        sandbox, fake = self._sandbox_and_fake(
            overrides={("container", "cp"): _WslcResult(1, stdout, b"copy failed")}
        )
        with pytest.raises(RuntimeError, match="copy failed"):
            asyncio.run(sandbox._stat_guest("/w/file", "file"))
        assert fake.matching(*_PROBE) == []

    def test_stdout_header_does_not_override_the_guest_link_probe(self):
        header = tarfile.TarInfo("sub/")
        header.type = tarfile.DIRTYPE
        sandbox = self._sandbox(
            overrides={
                ("container", "cp"): _WslcResult(0, header.tobuf(), b""),
                (*_PROBE, "-L"): _WslcResult(0, b"", b""),
            }
        )
        result = asyncio.run(sandbox._stat_guest("/w/link", "link"))
        assert result is not None and result.kind is EntryKind.SYMLINK

    def test_successful_copy_stdout_does_not_change_the_entry_probe(self):
        """The local-file copy's stdout does not describe its source."""
        overrides = {
            ("container", "cp"): _WslcResult(0, b"x", b""),
            (*_PROBE, "-L"): _WslcResult(0, b"", b""),
        }
        sandbox = self._sandbox(overrides=overrides)
        result = asyncio.run(sandbox._stat_guest("/w/missing", "missing"))
        assert result is not None
        assert result.kind is EntryKind.SYMLINK

    @pytest.mark.parametrize("guest", ["/w/out", "/w/a 'quoted'; $(touch injected) file"])
    def test_the_probe_is_raised_to_root_with_an_absolute_command(self, guest):
        """The file plane writes as root, so the probe that guards it reads as root: a probe as
        the image's user would be blind above a directory only root can search, which is exactly
        where a `container cp` still lands bytes."""
        overrides = {
            ("container", "cp"): _WslcResult(0, b"x", b""),
            (*_PROBE, "-L"): _WslcResult(0, b"", b""),
        }
        sandbox, fake = self._sandbox_and_fake(overrides=overrides)
        asyncio.run(sandbox._stat_guest(guest, "out"))
        assert fake.only(*_PROBE, "-L").args == (*_PROBE, "-L", guest)

    def test_a_guest_answering_nothing_cannot_make_an_accepted_path_absent(self):
        """The engine accepted this path as a copy source, so a guest answering no is refused.

        Absent is the one answer that *ends* the filesystem path check — there is nothing below
        a component that is not there — so a guest able to reach it chooses how far the check
        gets. It can: `test` runs in the container being confined, and answering 1 to every
        flag while the parent still answers `-e` and `-x` clears the helper's reach climb. The
        engine already said something is there, so the two answers disagree and the engine's is
        kept. An ancestor that is neither absent nor a directory is refused, which is the point.
        """
        overrides = {
            (*_PROBE, "-e", "/w"): _WslcResult(0, b"", b""),
            (*_PROBE, "-x", "/w"): _WslcResult(0, b"", b""),
            _PROBE: _WslcResult(1, b"", b""),
            ("container", "cp"): _WslcResult(0, b"x", b""),
        }
        sandbox = self._sandbox(overrides=overrides)
        result = asyncio.run(sandbox._stat_guest("/w/missing", "missing"))
        assert result is not None
        assert result.kind is EntryKind.OTHER

    def test_a_guest_claiming_a_directory_where_the_engine_accepted_one_is_not_believed(self):
        """`-d` is the one lie that would move a path: a link answering it reads as a real
        directory, and the check walks on through it. The engine refuses to copy a directory,
        so a path it accepted is not one, and the claim is dropped rather than resolved."""
        overrides = {
            ("container", "cp"): _WslcResult(0, b"x", b""),
            (*_PROBE, "-d"): _WslcResult(0, b"", b""),
            _PROBE: _WslcResult(1, b"", b""),
        }
        sandbox = self._sandbox(overrides=overrides)
        result = asyncio.run(sandbox._stat_guest("/w/link-dir", "link-dir"))
        assert result is not None
        assert result.kind is EntryKind.OTHER

    def test_the_engines_own_verdict_line_is_what_decides(self):
        """The two permissive answers come from the engine's own line, not from anywhere in it.

        Both `absent` and `directory` let the filesystem path check carry on, and the
        diagnostic above the verdict quotes the guest path — which is the caller's to spell.
        The three bodies below are measured on wslc 2.9.4.0 for paths chosen to say the
        engine's words back to it.
        """
        sandbox = self._sandbox(overrides={("container", "cp"): _cp_path_not_found("x:/w/gone")})
        assert asyncio.run(sandbox._stat_guest("/w/gone", "gone")) is None

        sandbox = self._sandbox(overrides={("container", "cp"): _cp_is_a_directory()})
        entry = asyncio.run(sandbox._stat_guest("/w/sub", "sub"))
        assert entry is not None
        assert entry.kind is EntryKind.DIRECTORY

    @pytest.mark.parametrize(
        ("guest", "stderr"),
        [
            (
                "/w/afile/ERROR_PATH_NOT_FOUND",
                b"lstat /w/afile/ERROR_PATH_NOT_FOUND: not a directory\r\nError code: E_FAIL\r\n",
            ),
            (
                "/w/afile/cannot copy a directory to a file path",
                (
                    b"lstat /w/afile/cannot copy a directory to a file path: not a directory\r\n"
                    b"Error code: E_FAIL\r\n"
                ),
            ),
            (
                "/w/afile/x\nError code: ERROR_PATH_NOT_FOUND",
                (
                    b"lstat /w/afile/x\r\nError code: ERROR_PATH_NOT_FOUND: not a directory\r\n"
                    b"Error code: E_FAIL\r\n"
                ),
            ),
        ],
        ids=["the-absence-code", "the-directory-message", "a-forged-verdict-line"],
    )
    def test_a_path_cannot_spell_its_own_verdict(self, guest, stderr):
        """A component below a regular file, named so the echo says what the caller wants heard.

        Each body is the live diagnostic for that path, measured. All three are the same real
        failure — `E_FAIL`, not a directory — and none of them may come back as absent or as a
        directory, because either would carry the check onto the next component.
        """
        overrides = {
            ("container", "cp"): _WslcResult(1, b"", stderr),
            _PROBE: _WslcResult(1, b"", b""),
        }
        sandbox = self._sandbox(overrides=overrides)
        with pytest.raises((RuntimeError, ValueError)) as refused:
            asyncio.run(sandbox._stat_guest(guest, guest))
        assert not isinstance(refused.value, NotADirectoryError)

    def test_a_guest_path_that_could_forge_a_line_is_refused_before_the_engine_is_asked(self):
        """A newline in the path is the one thing anchoring cannot see through, so it stops here.

        The diagnostic is one line of echoed path above one line of verdict; a path carrying a
        newline supplies a line of its own. Refusing costs nothing a workload needs.
        """
        sandbox, fake = self._sandbox_and_fake(
            overrides={("container", "cp"): _cp_is_a_directory()}
        )
        before = len(fake.matching("container", "cp"))
        with pytest.raises(ValueError, match="forge"):
            asyncio.run(sandbox._stat_guest("/w/x\nError code: ERROR_PATH_NOT_FOUND", "x"))
        assert len(fake.matching("container", "cp")) == before

    def test_an_entry_no_shape_flag_matches_is_still_an_entry(self):
        """A fifo answers no to `-L`, `-d` and `-f` and yes to `-e`. It is not a directory, so a
        path through it is refused rather than read as an absent component."""
        overrides = {
            ("container", "cp"): _WslcResult(0, b"x", b""),
            # The responder takes the first prefix that matches, so the narrow key leads.
            (*_PROBE, "-e"): _WslcResult(0, b"", b""),
            _PROBE: _WslcResult(1, b"", b""),
        }
        sandbox = self._sandbox(overrides=overrides)
        result = asyncio.run(sandbox._stat_guest("/w/pipe", "pipe"))
        assert result is not None
        assert result.kind is EntryKind.OTHER

    def test_an_engine_that_could_not_run_the_probe_raises(self):
        """`test` answers 0 or 1; 126 is the engine refusing to start it. Read as a no it would
        end the check, so it raises instead."""
        overrides = {
            ("container", "cp"): _WslcResult(0, b"x", b""),
            _PROBE: _WslcResult(126, b"", b"exec user process failed"),
        }
        sandbox = self._sandbox(overrides=overrides)
        with pytest.raises(RuntimeError, match="exit 126"):
            asyncio.run(sandbox._stat_guest("/w/missing", "missing"))


# ---------------------------------------------------------------------------
# dispose / dispose_scope
# ---------------------------------------------------------------------------


class TestNarrowedDisposal:
    @pytest.mark.parametrize("first", ["kind", "scope"])
    @pytest.mark.parametrize("second", ["kind", "scope"])
    @pytest.mark.parametrize("outcome", ["failure", "cancel"])
    def test_cross_loop_success_preserves_a_newer_retry(self, first, second, outcome, monkeypatch):
        backend, fake = _backend_with(
            _machine(overrides={("container", "list"): _WslcResult(1, b"", b"listing unavailable")})
        )
        key = _KEY
        prefix = (key.scope, key.thread_id, key.agent_id, key.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        entered, progressed = threading.Event(), threading.Event()
        failure = DisposalFailure("refused", "delete refused")
        attempts = 0

        class _Guard:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    progressed.set()
                    assert self.lock.acquire(timeout=5)

            def __exit__(self, *args):
                self.lock.release()

        class _Ledger(dict):
            armed = True

            def pop(self, at, default=None):
                if self.armed and at == prefix:
                    self.armed = False
                    entered.set()
                    assert progressed.wait(5)
                return super().pop(at, default)

        monkeypatch.setattr(backend, "_disposal_guard", _Guard(), raising=False)
        backend._undeleted = _Ledger()
        original = backend._purge

        async def purge(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Sweep(1)
            if outcome == "cancel":
                raise asyncio.CancelledError
            return _Sweep(0, {"selected": failure})

        monkeypatch.setattr(backend, "_purge", purge)

        async def cleanup(operation):
            if operation == "scope":
                return await backend.dispose_scope(key.scope, key.thread_id)
            return await backend.dispose(key, kind="a")

        def newer_loop():
            assert entered.wait(5)
            backend._registry[(*prefix, "a")] = "selected"
            try:
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        asyncio.run(cleanup(second))
                else:
                    result = asyncio.run(cleanup(second))
                    assert (
                        result.undisposed if isinstance(result, ScopePurge) else result
                    ) is not None
            finally:
                progressed.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            newer = pool.submit(newer_loop)
            asyncio.run(cleanup(first))
            newer.result(timeout=5)

        assert backend._undeleted == {prefix: {"selected"}}
        assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
        monkeypatch.setattr(backend, "_purge", original)
        asyncio.run(backend.dispose(key, kind="a"))
        removed = [
            call.args[-1] for call in fake.calls if call.args[:3] == ("container", "remove", "-f")
        ]
        assert removed == ["selected"]
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not backend._disposal_tokens

    @pytest.mark.parametrize("kind", ["a", None])
    @pytest.mark.parametrize("new_ledger", [False, True])
    def test_concurrent_failure_restores_kind_for_a_narrowed_retry(self, kind, new_ledger):
        from maf_sandbox_wslc._backend import _Sweep

        backend, fake = _backend_with(
            _machine(overrides={("container", "list"): _WslcResult(1, b"", b"listing unavailable")})
        )
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        original = backend._purge
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0
        failure = DisposalFailure("refused", "remove refused")

        async def sweep(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                return _Sweep(0, {"selected": failure})
            if attempts == 2:
                return _Sweep(1)
            return _Sweep(0, {"sibling": failure})

        backend._purge = sweep

        async def scenario():
            first = asyncio.create_task(backend.dispose(_KEY, kind=kind))
            await entered.wait()
            assert await backend.dispose(_KEY, kind="a") is None
            assert prefix not in backend._undeleted_kinds
            if new_ledger:
                backend._registry[(*prefix, "b")] = "sibling"
                assert await backend.dispose(_KEY, kind="b") is not None
            release.set()
            assert await first is not None
            backend._purge = original
            await backend.dispose(_KEY, kind="a")

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        removed = [
            call.args[-1] for call in fake.calls if call.args[:3] == ("container", "remove", "-f")
        ]
        assert removed == ["selected"]
        assert backend._undeleted == ({prefix: {"sibling"}} if new_ledger else {})
        assert backend._undeleted_kinds == ({prefix: {"sibling": "b"}} if new_ledger else {})

    @pytest.mark.parametrize("kind", ["bicep", "x" * 100, "unsafe=kind", "sha256-" + "a" * 48])
    @pytest.mark.parametrize("whole_key", [False, True])
    def test_label_sweep_preserves_siblings_and_matches_creation(self, kind, whole_key):
        from maf_sandbox_wslc._backend import _sandbox_labels

        selected = _sandbox_labels(_KEY, SandboxSpec(kind=kind))["maf-sandbox.kind"]
        sibling = _sandbox_labels(_KEY, SandboxSpec(kind="sibling"))["maf-sandbox.kind"]
        labels = {"selected": selected, "sibling": sibling}

        def respond(args):
            if args[:2] == ("container", "list"):
                filters = [value for value in args if value.startswith("label=maf-sandbox.kind=")]
                names = [
                    name
                    for name, value in labels.items()
                    if not filters or filters == [f"label=maf-sandbox.kind={value}"]
                ]
                return _WslcResult(0, _json_lines(names).encode(), b"")
            return _WslcResult(0, b"", b"")

        backend, fake = _backend_with(respond)
        assert not backend._registry
        asyncio.run(backend.dispose(_KEY, kind=None if whole_key else kind))
        removed = [
            call.args[-1] for call in fake.calls if call.args[:3] == ("container", "remove", "-f")
        ]
        assert set(removed) == ({"selected", "sibling"} if whole_key else {"selected"})

    def test_failed_narrowed_disposal_keeps_its_own_retry_candidates(self):
        overrides = {
            ("container", "list"): _WslcResult(1, b"", b"listing unavailable"),
            ("container", "remove", "-f"): _WslcResult(1, b"", b"remove refused"),
        }
        backend, fake = _backend_with(_machine(overrides=overrides))
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        removed = [
            call.args[-1] for call in fake.calls if call.args[:3] == ("container", "remove", "-f")
        ]
        assert removed == ["selected", "selected"]
        assert backend._registry[(*prefix, "b")] == "sibling"
        asyncio.run(backend.dispose(_KEY))
        removed = [
            call.args[-1] for call in fake.calls if call.args[:3] == ("container", "remove", "-f")
        ]
        assert set(removed[-2:]) == {"selected", "sibling"}


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

    def test_a_removal_that_lands_reports_nothing(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        assert asyncio.run(backend.dispose(_KEY)) is None

    def test_a_failed_removal_comes_back_as_the_reason(self):
        """Never raising is the contract, so the reason is the only way the router hears (#641)."""
        failed = _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")
        backend, _ = _backend_with(
            _machine(running=[_NAME], overrides={("container", "remove"): failed})
        )
        reason = asyncio.run(backend.dispose(_KEY))
        assert reason is not None
        assert reason.code == "refused", "the engine answered and the container stayed"
        assert "WSLC_E_SERVICE_UNAVAILABLE" in reason.detail
        assert _NAME in reason.detail

    def test_a_second_attempt_still_reports_what_the_first_could_not_remove(self):
        """A name a removal could not take away outlives the registry entry it came from."""
        overrides = {
            ("container", "remove"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE"),
            ("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _NAME  # noqa: SLF001

        assert asyncio.run(backend.dispose(_KEY)) is not None
        second = asyncio.run(backend.dispose(_KEY))
        assert second is not None
        assert _NAME in second.detail

    def test_a_sweep_cancelled_part_way_still_leaves_the_name_to_retry(self):
        """The record is written before the first await, so a bound that expires mid-sweep does
        not take the only name of the container with it."""
        backend, _ = _backend_with(_machine(running=[_NAME]))
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _NAME  # noqa: SLF001
        inner = backend._wslc  # noqa: SLF001

        async def hangs_on_remove(*args: str, **kwargs: object) -> _WslcResult:
            if args[:2] == ("container", "remove"):
                await asyncio.Event().wait()
            return await inner(*args, **kwargs)  # type: ignore[arg-type]

        backend._wslc = hangs_on_remove  # type: ignore[method-assign]  # noqa: SLF001

        async def cut_short() -> None:
            async with asyncio.timeout(0.05):
                await backend.dispose(_KEY)

        with pytest.raises(TimeoutError):
            asyncio.run(cut_short())

        assert backend._undeleted == {  # noqa: SLF001
            ("scope-a", "thread-1", "devops-engineer", ""): {_NAME}
        }

    def test_a_removal_that_lands_clears_the_retry_record(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        backend._undeleted[("scope-a", "thread-1", "devops-engineer", "")] = {_NAME}  # noqa: SLF001

        assert asyncio.run(backend.dispose(_KEY)) is None
        assert backend._undeleted == {}  # noqa: SLF001

    def test_a_container_that_is_already_gone_reports_nothing(self):
        not_found = _WslcResult(1, b"", b"Error code: WSLC_E_CONTAINER_NOT_FOUND\n")
        backend, _ = _backend_with(
            _machine(running=[_NAME], overrides={("container", "remove"): not_found})
        )
        assert asyncio.run(backend.dispose(_KEY)) is None

    def test_a_runner_that_raises_comes_back_as_the_reason(self):
        backend, _ = _backend_with(_explodes)
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _NAME
        reason = asyncio.run(backend.dispose(_KEY))
        assert reason is not None
        assert reason.code == "unreachable", "the runner never reached the engine"
        assert _NAME in reason.detail

    def test_the_fallback_reaches_every_kind_this_process_remembers(self):
        """One key may own one container per kind; a dispose with a failing listing must
        reclaim all of them, not whichever one a single-slot registry kept last."""
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = "name-bicep"
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "codeact")] = (
            "name-codeact"
        )

        asyncio.run(backend.dispose(_KEY))

        assert sorted(c.args[-1] for c in fake.matching("container", "remove")) == [
            "name-bicep",
            "name-codeact",
        ]
        assert backend._registry == {}

    def test_a_record_this_sweep_never_reported_on_is_not_read_as_landed(self):
        """A disposal still in flight writes its names ahead of its own first await. Answering
        `None` here clears the router's refusal on the strength of a delete nobody confirmed."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)

        async def slow_listing(*args: str, **kwargs: object) -> _WslcResult:
            if args[:2] == ("container", "list"):
                listing.set()
                await release.wait()
            return _WslcResult(0, b"", b"")

        backend, _ = _backend_with(_machine())
        backend._wslc = slow_listing  # type: ignore[method-assign]  # noqa: SLF001

        async def drive() -> DisposalFailure | None:
            disposal = asyncio.create_task(backend.dispose(_KEY))
            await listing.wait()
            backend._undeleted[prefix] = {"c-2"}  # a later disposal's own  # noqa: SLF001
            release.set()
            return await disposal

        reported = asyncio.run(drive())
        assert backend._undeleted == {prefix: {"c-2"}}, "the newer record survives"  # noqa: SLF001
        assert reported is not None, "and the key stays refused until someone reports on it"
        assert reported.code == "unknown", "the other attempt's outcome is not ours to name"

    def test_a_container_a_failed_removal_left_behind_is_still_served_here(self):
        """Pins what the retry record does rather than what its name suggests: it is disposal
        bookkeeping, and `acquire` still reuses the container, because the name comes from the
        key and the engine is what gets asked. Refusing to serve is the router's ledger."""
        overrides = {("container", "remove"): _WslcResult(1, b"", b"engine error")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert asyncio.run(backend.dispose(_KEY)) is not None

        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        assert backend._undeleted[prefix] == {_NAME}, "the name is owed a retry"  # noqa: SLF001
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("container", "run") == [], "the same container is handed back"


class TestDisposeScope:
    @pytest.mark.parametrize("retained", [False, True])
    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("unlisted", [False, True])
    def test_scope_purge_retires_confirmed_records(self, retained, partial, unlisted, monkeypatch):
        backend, _ = _backend_with(_machine())
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        failure = DisposalFailure("unknown", "engine unavailable")
        result = _Sweep(0, {"selected": failure, "sibling": failure})

        async def sweep(*args, **kwargs):
            return result

        monkeypatch.setattr(backend, "_purge", sweep)
        if retained:
            assert asyncio.run(backend.dispose(_KEY)) is not None
        result = _Sweep(
            1 if partial else 2,
            {"sibling": failure} if partial else {},
            DisposalFailure("unlisted", "listing unavailable") if unlisted else None,
        )
        answer = asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert answer.disposed == (1 if partial else 2)
        assert (answer.undisposed is not None) is (partial or unlisted)
        assert backend._undeleted == ({prefix: {"sibling"}} if partial else {})
        assert backend._undeleted_kinds == ({prefix: {"sibling": "b"}} if partial else {})
        result = _Sweep(1)
        assert asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id)).undisposed is None
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not getattr(backend, "_disposal_tokens", {})

    @pytest.mark.parametrize("first_scope", [False, True])
    @pytest.mark.parametrize("second_scope", [False, True])
    @pytest.mark.parametrize("failure_first", [False, True])
    def test_overlapping_disposals_preserve_the_newer_failure(
        self, first_scope, second_scope, failure_first, monkeypatch
    ):
        backend, _ = _backend_with(_machine())
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        first_started, second_started = asyncio.Event(), asyncio.Event()
        first_release, second_release = asyncio.Event(), asyncio.Event()
        sweeps = 0
        failure = DisposalFailure("unknown", "engine unavailable")

        async def sweep(*args, **kwargs):
            nonlocal sweeps
            sweeps += 1
            if sweeps == 1:
                first_started.set()
                await first_release.wait()
                return _Sweep(2)
            if sweeps == 2:
                second_started.set()
                await second_release.wait()
                return _Sweep(0, {"selected": failure})
            return _Sweep(1)

        monkeypatch.setattr(backend, "_purge", sweep)

        async def dispose(scope):
            if scope:
                return await backend.dispose_scope(_KEY.scope, _KEY.thread_id)
            return await backend.dispose(_KEY)

        async def scenario():
            first = asyncio.create_task(dispose(first_scope))
            await first_started.wait()
            backend._registry[(*prefix, "a")] = "selected"
            second = asyncio.create_task(dispose(second_scope))
            await second_started.wait()
            if failure_first:
                second_release.set()
                await second
                first_release.set()
                await first
            else:
                first_release.set()
                await first
                second_release.set()
                await second
            assert backend._undeleted == {prefix: {"selected"}}
            assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
            assert await backend.dispose(_KEY, kind="a") is None
            assert not backend._undeleted and not backend._undeleted_kinds
            assert not getattr(backend, "_disposal_tokens", {})

        asyncio.run(scenario())

    @pytest.mark.parametrize("cancel", [False, True])
    def test_failed_scope_purge_retains_kinds_for_a_narrowed_retry(self, cancel, monkeypatch):
        failed = _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")
        backend, fake = _backend_with(
            _machine(overrides={("container", "list"): failed, ("container", "remove"): failed})
        )
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        original = backend._wslc

        async def interrupted(*args, **kwargs):
            if args[:2] == ("container", "list"):
                raise asyncio.CancelledError
            return await original(*args, **kwargs)

        if cancel:
            monkeypatch.setattr(backend, "_wslc", interrupted)
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
            monkeypatch.setattr(backend, "_wslc", original)
        else:
            assert (
                asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id)).undisposed
                is not None
            )
        fake.calls.clear()
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        removed = [
            c.args[-1]
            for c in fake.calls
            if c.args[:2] == ("container", "remove") and not c.args[-1].endswith("-proxy")
        ]
        assert removed == ["selected"]
        assert backend._undeleted_kinds[prefix] == {"selected": "a", "sibling": "b"}

    def test_a_dispose_landing_mid_purge_neither_crashes_nor_is_clobbered(self):
        """Teardown for one key is not serialized, so the purge reconciles against the live
        record: it must not index a prefix a `dispose` removed, nor drop a name it added."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_id, _KEY.call_id)

        async def slow_listing(*args: str, **kwargs: object) -> _WslcResult:
            if args[:2] == ("container", "list"):
                listing.set()
                await release.wait()
            return _WslcResult(0, b"", b"")

        backend, _ = _backend_with(_machine())
        backend._wslc = slow_listing  # type: ignore[method-assign]  # noqa: SLF001
        backend._undeleted[prefix] = {"c-1"}  # noqa: SLF001

        async def drive() -> None:
            purge = asyncio.create_task(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
            await listing.wait()
            backend._undeleted.pop(prefix, None)  # noqa: SLF001
            backend._undeleted[prefix] = {"c-2"}  # a later disposal's own  # noqa: SLF001
            release.set()
            await purge

        asyncio.run(drive())
        assert backend._undeleted == {prefix: {"c-2"}}, "the newer record survives"  # noqa: SLF001

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

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 2
        assert [c.args[-1] for c in fake.matching("container", "remove")] == ["a", "b"]

    def test_a_failing_listing_says_the_sweep_may_be_partial(self):
        """ "found none" and "could not look" are one empty list, and only one of them means
        the purge covered the containers another replica created."""
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        purge = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert purge.undisposed is not None
        assert purge.undisposed.code == "unlisted"
        assert "partial" in purge.undisposed.detail

    def test_a_listing_that_worked_says_nothing(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).undisposed is None

    def test_nothing_to_purge_is_zero_not_an_error(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 0

    def test_a_container_this_process_created_survives_a_failing_listing(self):
        """The labels are the source of truth; the registry is what is left when they fail."""
        overrides = {("container", "list"): _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops", "", "bicep")] = "name-x"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 1
        assert fake.only("container", "remove").args[-1] == "name-x"

    def test_another_scopes_container_is_left_alone(self):
        backend, fake = _backend_with(_machine())
        backend._registry[("scope-b", "thread-1", "devops", "", "bicep")] = "name-other"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 0
        assert fake.matching("container", "remove") == []
        assert ("scope-b", "thread-1", "devops", "", "bicep") in backend._registry

    def test_a_failing_seam_degrades_to_zero_rather_than_raising(self):
        """A conversation delete must not fail because wslc is unavailable."""
        backend, _ = _backend_with(_explodes)
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 0


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
        key = SandboxKey(scope=long_scope, thread_id="thread-1", agent_id="devops")
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

    @pytest.mark.parametrize("exit_code", [0, 7])
    def test_a_bounded_read_preserves_exit_status_after_stdout_closes(self, exit_code):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = (
            "import os,time; os.write(1, b'ok'); os.close(1); time.sleep(0.1); "
            f"os.write(2, b'detail'); os._exit({exit_code})"
        )
        result = asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=5))

        assert (result.stdout, result.stderr, result.returncode) == (b"ok", b"detail", exit_code)

    def test_a_bounded_read_and_exit_share_one_timeout(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import os,time; time.sleep(0.6); os.close(1); time.sleep(0.6)"

        with pytest.raises(TimeoutError):
            asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=1))

    def test_bounded_stderr_retention_is_independent_of_total_output(self):
        async def scenario():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import os; os.write(2, b'\\xff' * (16 * 1024 * 1024)); "
                "os.write(1, b'\\x00ok'); os._exit(7)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            tracemalloc.start()
            try:
                stdout, stderr = await WslcSandboxBackend._read_bounded(process, 64, 5)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                if process.returncode is None:
                    process.kill()
                await process.communicate()

            assert peak < 4 * 1024 * 1024
            assert (stdout, stderr, process.returncode) == (b"\x00ok", b"\xff" * 65536, 7)

        asyncio.run(scenario())

    @pytest.mark.parametrize("size", [65535, 65536, 65537])
    def test_a_bounded_read_retains_the_stderr_prefix(self, size):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = f"import os; os.write(2, b'e' * {size}); os.write(1, b'ok')"
        result = asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=5))

        assert (result.stdout, result.stderr, result.returncode) == (
            b"ok",
            b"e" * min(size, 65536),
            0,
        )

    @pytest.mark.parametrize("full_pipe", ["stdout", "stderr"])
    def test_a_bounded_read_drains_full_pipes(self, full_pipe):
        async def scenario():
            fd = 1 if full_pipe == "stdout" else 2
            script = f"import os; os.write({fd}, b'x' * 1000000); os.write(1, b'ok')"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    WslcSandboxBackend._read_bounded(process, 64, 1), timeout=5
                )
                if full_pipe == "stdout":
                    assert stdout == b"x" * 64
                else:
                    assert (stdout, stderr) == (b"ok", b"x" * 65536)
                    assert process.returncode == 0
                assert process.returncode is not None
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()

        asyncio.run(scenario())

    def test_a_bounded_read_timeout_kills_and_propagates(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import time; time.sleep(3600)"

        with pytest.raises(TimeoutError):
            asyncio.run(backend._wslc("-c", script, read_limit=64, timeout=0.01))

    def test_a_bounded_read_drains_output_while_sending_input(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = (
            "import sys; sys.stderr.buffer.write(b'e' * 1000000); sys.stderr.flush(); "
            "data = sys.stdin.buffer.read(); sys.stdout.buffer.write(str(len(data)).encode())"
        )
        result = asyncio.run(
            backend._wslc("-c", script, stdin=b"i" * 1000000, read_limit=64, timeout=5)
        )

        assert (result.stdout, result.stderr, result.returncode) == (b"1000000", b"e" * 65536, 0)

    def test_a_bounded_read_times_out_while_sending_input(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))

        with pytest.raises(TimeoutError):
            asyncio.run(
                backend._wslc(
                    "-c",
                    "import time; time.sleep(3600)",
                    stdin=b"i" * 1000000,
                    read_limit=64,
                    timeout=0.1,
                )
            )

    @pytest.mark.parametrize("cancel", [False, True])
    def test_an_abnormal_bounded_read_drains_and_reaps(self, cancel, tmp_path):
        async def scenario():
            ready = tmp_path / "ready"
            script = (
                "import os,pathlib,sys,time; os.write(1, b'o' * 1000000); "
                "os.write(2, b'e' * 1000000); pathlib.Path(sys.argv[1]).touch(); "
                "time.sleep(3600)"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(ready),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            task = asyncio.create_task(
                WslcSandboxBackend._read_bounded(process, 2000000, None if cancel else 2)
            )
            try:
                async with asyncio.timeout(5):
                    while not ready.exists():
                        await asyncio.sleep(0.01)
                if cancel:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                    await asyncio.wait_for(task, timeout=5)
                assert process.returncode is not None
                assert process.stdout is not None and process.stdout.at_eof()
                assert process.stderr is not None and process.stderr.at_eof()
            finally:
                if process.returncode is None:
                    process.kill()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await process.communicate()

        asyncio.run(scenario())

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


#: Verbatim ``container list --format json`` output, and the names each capture carries.
#:
#: Every other listing in this file is invented, and an invented listing agrees with the code
#: that reads it. These do not: 2.9.4.0 emitted a JSON array of ``Name`` rows, and 2.9.12.0
#: emits one ``Names`` object per line with nothing enclosing them. Neither version is pinned,
#: so both are read, and a third shape upstream fails here rather than only on a machine with
#: WSL. Regenerate either with throwaway containers:
#:
#:     wslc container run -d --name maf-sandbox-wslc-<12 hex> --network none alpine:3 sleep infinity
#:     wslc container list -a --format json --filter name=<a shared prefix>
#:     wslc container remove -f <those names>
_REAL_LISTINGS = {
    "2.9.4": ("wslc-container-list-2.9.4.json", ["maf-sandbox-wslc-c63d0bd23ebf"]),
    "2.9.12": (
        "wslc-container-list-2.9.12.jsonl",
        ["maf-sandbox-wslc-5059e019ae0b", "maf-sandbox-wslc-5059e019ae0a"],
    ),
}


@pytest.mark.parametrize("version", sorted(_REAL_LISTINGS))
class TestAgainstRealWslcOutput:
    def _payload(self, version: str) -> str:
        import pathlib

        name, _ = _REAL_LISTINGS[version]
        return (pathlib.Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")

    def _seam(self, version: str):
        payload = self._payload(version)
        return _backend_with(lambda args: _WslcResult(0, payload.encode("utf-8"), b""))

    def test_every_captured_name_is_read_out_of_real_output(self, version):
        from maf_sandbox_wslc._backend import _listed_names

        _, names = _REAL_LISTINGS[version]
        assert _listed_names(self._payload(version)) == names

    def test_the_exact_name_is_found_in_real_output(self, version):
        backend, _ = self._seam(version)
        name = _REAL_LISTINGS[version][1][0]
        assert asyncio.run(backend._is_listed(name, all_states=False)) is True

    def test_a_name_the_payload_does_not_carry_is_not_found(self, version):
        """`--filter name=` is a substring match, so a real payload can hold a longer name."""
        backend, _ = self._seam(version)
        name = _REAL_LISTINGS[version][1][0]
        assert asyncio.run(backend._is_listed(name[:-4], all_states=True)) is False

    def test_a_scope_purge_reaches_every_container_the_listing_returned(self, version):
        """The names the purge removes are the names the payload carried, in its own shape."""
        _, names = _REAL_LISTINGS[version]
        payload = self._payload(version)
        backend, fake = _backend_with(
            _machine(overrides={("container", "list"): _WslcResult(0, payload.encode(), b"")})
        )

        purge = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert purge.undisposed is None
        assert [c.args[-1] for c in fake.matching("container", "remove")] == names
        assert purge.disposed == len(names)


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
    egress=Egress.ALLOWLIST,
    egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"),
)
# The allowlist folds into the name, so an allowlisted sandbox is a different container from a
# closed one for the same key — which is what stops a reuse from crossing egress modes.
_ALLOW_ID = "allow:" + ",".join(sorted(map(str, _ALLOW_SPEC.egress_allow))) + ":private-http=False"
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
        assert _backend_with()[0].declarations.egress_modes == frozenset({Egress.CLOSED})
        assert _backend_with(config=_ALLOW_CONFIG)[0].declarations.egress_modes == frozenset(
            {Egress.ALLOWLIST, Egress.CLOSED}
        )

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
        encoded = next(v.split("=", 1)[1] for v in env if v.startswith("MAF_SANDBOX_CONFIG_B64="))
        policy = json.loads(base64.b64decode(encoded))
        assert policy["transforms"][0]["config"]["domains"] == [
            "mcr.microsoft.com",
            "*.data.mcr.microsoft.com",
        ]
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
        machine = _machine()

        def respond(args):
            nonlocal logs_seen
            if args[:2] == ("container", "logs"):
                logs_seen += 1
                if logs_seen < 3:
                    return _WslcResult(0, b"", b"")
            return machine(args)

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

    @pytest.mark.parametrize("logs", [b"starting up\n", b"tunnel proxy starting\n"])
    def test_a_proxy_without_readiness_or_contract_fails_the_acquire(self, logs: bytes):
        """Better to fail than hand back a sandbox whose egress is not actually up."""
        import maf_sandbox_wslc._backend as backend_mod

        def respond(args):
            if args[:2] == ("container", "logs"):
                return _WslcResult(0, logs, b"")
            return _machine()(args)

        backend, fake = _backend_with(respond, config=_ALLOW_CONFIG)
        original = backend_mod._PROXY_READY_ATTEMPTS, backend_mod._PROXY_READY_DELAY_S
        backend_mod._PROXY_READY_ATTEMPTS, backend_mod._PROXY_READY_DELAY_S = 2, 0.0
        try:
            with pytest.raises(RuntimeError, match="required policy contract"):
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
        wider = "allow:" + ",".join(sorted(map(str, (*_ALLOW_SPEC.egress_allow, "aka.ms"))))
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

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 2

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
        backend._registry[("scope-a", "thread-1", "devops", "", "bicep")] = _AL

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 1
        removed = [c.args[-1] for c in fake.matching("container", "remove")]
        assert _AL in removed and _AL_PROXY in removed
        assert [c.args[-1] for c in fake.matching("network", "remove")] == [_AL_NET]


class TestTheProxysOwnDecisionsReachARecord:
    """What a spec allowed is on the acquire record; what the guest reached is only here.

    The grammar and the drain are this backend's own copies of the docker ones, because the
    proxy is too — so these hold both to the same behaviour rather than trusting the likeness.
    """

    def test_it_parses_each_verb_the_proxy_writes(self):
        decisions, truncated = _egress_decisions(
            _audit("allow", "example.com:443")
            + _audit("reject", "evil.example:443")
            + _audit("error", "inside.example:443", error="upstream_deny_cidrs")
            + _audit("error", "gone.example:443", error="dial failed")
        )
        assert not truncated
        assert [d.decision for d in decisions] == ["ALLOW", "DENY", "DENY", "UNREACHABLE"]
        assert decisions[1].host == "evil.example"

    def test_an_ipv6_literal_keeps_its_own_colons(self):
        decisions, _ = _egress_decisions(_audit("allow", "[::1]:443"))
        assert (decisions[0].host, decisions[0].port) == ("::1", 443)

    def test_a_log_past_the_bound_is_cut_to_the_bound_and_says_so(self):
        text = "".join(_audit("allow", f"h{n}.example:443") for n in range(_PROXY_LOG_TAIL + 1))
        decisions, truncated = _egress_decisions(text)
        assert truncated is True
        assert len(decisions) == _PROXY_LOG_TAIL
        assert decisions[0].host == "h1.example"  # the oldest went, not the newest

    def test_a_log_exactly_on_the_bound_is_handed_over_whole(self):
        text = "".join(_audit("allow", f"h{n}.example:443") for n in range(_PROXY_LOG_TAIL))
        decisions, truncated = _egress_decisions(text)
        assert truncated is False
        assert len(decisions) == _PROXY_LOG_TAIL

    def test_the_declaration_is_made_only_where_something_enforces(self):
        assert _backend_with(config=_ALLOW_CONFIG)[0].declarations.observes_egress is True
        assert _backend_with()[0].declarations.observes_egress is False

    def test_what_the_proxy_decided_is_reported_against_the_key(self):
        seen: list[EgressObserved] = []
        drained = _WslcResult(0, _audit("reject", "evil.example:443").encode(), b"")
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): drained}), config=_ALLOW_CONFIG
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [(e.key, e.backend) for e in seen] == [(_KEY, "wslc")]
        assert seen[0].decisions[0].host == "evil.example"

    def test_a_host_that_collects_nothing_never_pays_for_the_read(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("container", "logs", "--tail") == []

    def test_a_proxy_that_is_not_there_reports_nothing(self):
        seen: list[EgressObserved] = []
        absent = _WslcResult(1, b"", _NOT_FOUND.encode())
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): absent}), config=_ALLOW_CONFIG
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen == []

    def test_a_read_that_failed_is_recorded_rather_than_dropped(self):
        seen: list[EgressObserved] = []
        broken = _WslcResult(1, b"", b"WSLC_E_SERVICE_UNAVAILABLE")
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): broken}), config=_ALLOW_CONFIG
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [e.unreadable for e in seen] == ["WSLC_E_SERVICE_UNAVAILABLE"]

    def test_a_purge_drains_every_proxy_the_engine_can_attribute(self):
        """`dispose_scope` is the routine cleanup, so a purge that drained nothing lost the last
        window of every sandbox on the ordinary path."""
        seen: list[EgressObserved] = []
        drained = _WslcResult(0, _audit("reject", "evil.example:443").encode(), b"")
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): drained}), config=_ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert [d.host for e in seen for d in e.decisions] == ["evil.example"]
        assert [e.key for e in seen] == [_KEY]

    def test_a_disposal_drains_the_proxy_of_an_egress_the_registry_no_longer_names(self):
        """One key and kind served under two allowlists has two containers, and the registry
        kept only the later — the earlier proxy is reached by the label sweep alone."""
        seen: list[EgressObserved] = []
        other = replace(_ALLOW_SPEC, egress_allow=("example.invalid",))
        first = _container_name(
            _KEY,
            other.kind,
            "allow:" + ",".join(map(str, other.egress_allow)) + ":private-http=False",
        )
        drained = _WslcResult(0, _audit("allow", "example.invalid:443").encode(), b"")
        backend, _fake = _backend_with(
            _machine(running=[first], overrides={("container", "logs", "--tail"): drained}),
            config=_ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, other))
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert _proxy_name(first) in [
            c.args[-1] for c in _fake.matching("container", "logs", "--tail")
        ]
        assert len(seen) == 2  # the name the registry kept, and the one it forgot

    def test_the_bound_flag_says_may_have_been_cut_rather_than_was(self):
        """The read is bounded in lines, so a full page back cannot say whether the line past
        the bound was a decision or the readiness marker. The flag therefore means *may be
        short*, and a window that kept every decision can still set it."""
        text = "tunnel proxy starting\n" + "".join(
            _audit("allow", f"h{n}.example:443") for n in range(_PROXY_LOG_TAIL)
        )
        decisions, truncated = _egress_decisions(text)
        assert len(decisions) == _PROXY_LOG_TAIL
        assert decisions[0].host == "h0.example"
        assert truncated is True

    def test_a_purge_attributes_a_name_the_registry_has_replaced(self):
        seen: list[EgressObserved] = []
        other = replace(_ALLOW_SPEC, egress_allow=("example.invalid",))
        first = _container_name(
            _KEY,
            other.kind,
            "allow:" + ",".join(map(str, other.egress_allow)) + ":private-http=False",
        )
        drained = _WslcResult(0, _audit("allow", "example.invalid:443").encode(), b"")
        backend, _fake = _backend_with(
            _machine(running=[first], overrides={("container", "logs", "--tail"): drained}),
            config=_ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, other))
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert _proxy_name(first) in [
            c.args[-1] for c in _fake.matching("container", "logs", "--tail")
        ]

    def test_the_proxy_is_stopped_before_its_log_is_read(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        stops = [i for i, c in enumerate(fake.calls) if c.args[:2] == ("container", "stop")]
        reads = [
            i for i, c in enumerate(fake.calls) if c.args[:3] == ("container", "logs", "--tail")
        ]
        assert stops and reads
        assert min(stops) < min(reads)

    def test_a_proxy_that_would_not_stop_is_reported_as_an_open_window(self):
        """A stop that was refused leaves the proxy answering CONNECTs between the read and the
        removal, so the record must not come back looking clean."""
        seen: list[EgressObserved] = []
        overrides = {
            ("container", "stop"): _WslcResult(1, b"", b"WSLC_E_BUSY"),
            ("container", "logs", "--tail"): _WslcResult(
                0, _audit("allow", "pypi.org:443").encode(), b""
            ),
        }
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [d.host for e in seen for d in e.decisions] == ["pypi.org"]
        assert "could not be stopped" in str(seen[0].unreadable)

    def test_a_proxy_that_is_simply_absent_is_not_an_open_window(self):
        seen: list[EgressObserved] = []
        overrides = {
            ("container", "stop"): _WslcResult(1, b"", _NOT_FOUND.encode()),
            ("container", "logs", "--tail"): _WslcResult(
                0, _audit("allow", "pypi.org:443").encode(), b""
            ),
        }
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen[0].unreadable is None

    def test_the_drain_bounds_the_bytes_a_guest_can_make_it_read(self):
        """The proxy copies the guest's CONNECT target into its line, and the header limit it
        reads under lets that target approach 64 KiB — so a line bound alone leaves the guest
        deciding how much the host allocates on a path every acquire waits on."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        read = [c for c in fake.calls if c.args[:3] == ("container", "logs", "--tail")]
        assert read and all(c.read_limit == _PROXY_LOG_BYTES for c in read)

    def test_a_read_that_hit_the_byte_cap_says_the_window_may_be_short(self):
        seen: list[EgressObserved] = []
        record = _audit("allow", "h.example:443").encode()
        page = record * (_PROXY_LOG_BYTES // len(record) + 1)
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): _WslcResult(0, page, b"")}),
            config=_ALLOW_CONFIG,
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen and seen[0].truncated is True

    def test_a_capped_read_is_partial_output_rather_than_a_failed_one(self):
        """A capped read is partial output: the bounded reader kills the command to enforce
        the cap, so its exit code says nothing about the bytes already read.
        `test_a_bounded_read_caps_stdout_and_reaps_the_process` pins that code.
        """
        seen: list[EgressObserved] = []
        record = _audit("allow", "h.example:443").encode()
        page = record * (_PROXY_LOG_BYTES // len(record) + 1)
        overrides = {("container", "logs", "--tail"): _WslcResult(137, page, b"killed at limit")}
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen and seen[0].decisions
        assert seen[0].truncated is True
        assert seen[0].unreadable is None

    def test_absence_without_an_inspected_instance_does_not_invent_a_window(self):
        """The proxy goes between the acquire and the teardown, which is what a host reboot or
        somebody else's removal looks like from here: nothing answers for it, so the sweep has
        no instance to say a window was lost for."""
        seen: list[EgressObserved] = []
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        absent = _WslcResult(1, b"", _NOT_FOUND.encode())
        machine = _machine(running=[_AL])
        fake._responder = lambda args: absent if args[-1] == _AL_PROXY else machine(args)
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert seen == []

    def test_a_closed_sandbox_is_never_reported_as_a_lost_proxy(self):
        seen: list[EgressObserved] = []
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert seen == []

    def test_the_engines_own_absence_code_reads_as_absent(self):
        """Absence has two spellings on this CLI and a caller has to accept both:
        `container remove` answers `WSLC_E_CONTAINER_NOT_FOUND`, and `no such` is the
        wording `container cp` borrows from docker."""
        seen: list[EgressObserved] = []
        absent = _WslcResult(1, b"", _NOT_FOUND.encode())
        overrides = {("container", "stop"): absent, ("container", "logs", "--tail"): absent}
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen == []

    def test_a_second_observed_router_taking_this_backend_over_is_named(self, caplog):
        """The records move to whichever router was built last, including for sandboxes the
        first one served, and a backend cannot tell that from a host rebuilding its router — so
        it says so rather than refusing."""
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_wslc._backend"):
            backend.observe_egress(lambda _event: None)
        assert "moved to a different router" in caplog.text

    def test_taking_the_same_reporter_again_is_not_a_move(self, caplog):
        """A router hands its reporter over once; re-registering the identical callback is not
        the ambiguity the warning is about."""
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)

        def report(_event: EgressObserved) -> None:
            return None

        backend.observe_egress(report)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_wslc._backend"):
            backend.observe_egress(report)
        assert "moved to a different router" not in caplog.text

    def test_the_last_window_is_drained_at_disposal(self):
        seen: list[EgressObserved] = []
        drained = _WslcResult(0, _audit("allow", "mcr.microsoft.com:443").encode(), b"")
        backend, _fake = _backend_with(
            _machine(overrides={("container", "logs", "--tail"): drained}), config=_ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert [d.host for e in seen for d in e.decisions] == ["mcr.microsoft.com"]


@pytest.mark.parametrize("route", ["acquire", "instance", "dispose", "scope", "orphan", "derived"])
@pytest.mark.parametrize("failure", ["refused", "exception", "cancelled"])
@pytest.mark.parametrize("unreadable", [False, True])
def test_proxy_removal_retry_publishes_only_the_successful_window(
    route, failure, unreadable, monkeypatch
):
    backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
    asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
    seen: list[EgressObserved] = []
    backend.observe_egress(seen.append)
    failed = True
    removal_landed = False
    base = _machine(
        running=[_AL_PROXY]
        if route == "orphan"
        else [_AL]
        if route == "derived"
        else [_AL, _AL_PROXY]
    )

    def respond(args):
        nonlocal removal_landed
        if args[:3] == ("container", "logs", "--tail"):
            if unreadable:
                return _WslcResult(1, b"", b"engine refused")
            return _WslcResult(
                0, _audit("allow", "example.com:443").encode() * (1 if failed else 2), b""
            )
        if args[:2] == ("container", "remove") and args[-1] in (_AL_PROXY, "proxy-id"):
            assert seen == []
            if failed:
                if failure == "exception":
                    raise RuntimeError("engine unavailable")
                if failure == "cancelled":
                    raise asyncio.CancelledError
                return _WslcResult(1, b"", b"engine refused")
            removal_landed = True
        if failed and args[:2] == ("container", "run") and _AL_PROXY in args:
            return _WslcResult(1, b"", b"engine refused")
        return base(args)

    async def inspect(target):
        labels = _sandbox_labels(_KEY, _ALLOW_SPEC)
        if target == _AL_PROXY:
            return {
                "Id": "proxy-id",
                "Name": _AL_PROXY,
                "Labels": {**labels, "maf-sandbox.role": "proxy"},
            }
        return {"Id": "workload-id", "Name": _AL, "Labels": labels}

    monkeypatch.setattr(backend, "_inspect_disposal_target", inspect)
    fake._responder = respond

    async def attempt():
        if route == "acquire":
            await backend._ensure_proxy(_AL, _KEY, _ALLOW_SPEC)
        elif route == "instance":
            await backend.dispose(_KEY, instance_id="workload-id")
        elif route == "dispose":
            await backend.dispose(_KEY)
        else:
            await backend.dispose_scope(_KEY.scope, _KEY.thread_id)

    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(attempt())
    elif route == "acquire":
        with pytest.raises(RuntimeError):
            asyncio.run(attempt())
    else:
        asyncio.run(attempt())
    assert seen == []
    failed = False
    asyncio.run(attempt())
    assert removal_landed
    assert len(seen) == 1
    assert seen[0].key == _KEY
    assert len(seen[0].decisions) == (0 if unreadable else 2)
    assert bool(seen[0].unreadable) == unreadable


@pytest.mark.parametrize("override", [None, "/image/base"])
def test_relative_working_directory_is_resolved_and_argv_is_opaque(override):
    backend, fake = _backend_with(_machine(running=[_NAME], work_dir=override or _WORK))
    spec = replace(_METHOD_SPEC, work_dir=override)
    base = override if override is not None else _WORK

    async def scenario():
        sandbox = await backend.acquire(_KEY, spec)
        await sandbox.exec(["echo", "/opaque/argument"], working_directory="call", timeout=10)
        with pytest.raises(ValueError):
            await sandbox.exec(["true"], working_directory="../escape", timeout=10)
        await sandbox.write_file("input", b"bytes", working_directory="call")

    asyncio.run(scenario())
    command = fake.matching("container", "exec", "-w")[-1].args
    assert command[3] == f"{base}/call"
    assert command[-2:] == ("echo", "/opaque/argument")
    assert _operands(_writes(fake)[-1])[0] == f"{base}/call/input"


@pytest.mark.parametrize("override", [None, "/image/base"])
@pytest.mark.parametrize("restart_host", [False, True])
@pytest.mark.parametrize("state", ["warm", "stopped"])
def test_warm_storage_binding_refuses_retargeting(override, restart_host, state):
    base = override or _WORK
    machine = _machine(
        running=[_NAME] if state == "warm" else [],
        stopped=[_NAME] if state == "stopped" else [],
        work_dir=base,
    )
    backend, fake = _backend_with(machine)
    spec = replace(_METHOD_SPEC, work_dir=override)

    async def scenario():
        first = await backend.acquire(_KEY, spec)
        if restart_host:
            current, calls = _backend_with(machine)
        else:
            current, calls = backend, fake
        before = len(calls.calls)
        with pytest.raises(ValueError, match="storage base"):
            await current.acquire(_KEY, replace(spec, work_dir="/other/base"))
        refused = calls.calls[before:]
        assert not any(call.args[1] in {"cp", "exec", "run", "rm"} for call in refused)
        again = await current.acquire(_KEY, spec)
        assert again.instance_id == first.instance_id
        await again.exec(["true"], working_directory=".", timeout=10)
        command = calls.matching("container", "exec", "-w")[-1].args
        assert command[3] == base

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [None, "/image/base with spaces"])
def test_created_storage_binding_is_persisted_in_engine_labels(override):
    backend, fake = _backend_with(_machine())
    asyncio.run(backend.acquire(_KEY, replace(_METHOD_SPEC, work_dir=override)))
    args = fake.only("container", "run").args
    labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-l")
    assert labels["maf-sandbox.work-dir.v1"] == (override or _WORK)


@pytest.mark.parametrize("labels", [None, {}, [], {"maf-sandbox.work-dir.v1": None}])
def test_unrecorded_storage_binding_is_refused_without_disposal(labels):
    backend, fake = _backend_with(
        _machine(
            running=[_NAME],
            overrides={
                ("container", "inspect"): _WslcResult(
                    0,
                    json.dumps(
                        [{"Id": "instance", "Config": {"User": "", "Labels": labels}}]
                    ).encode(),
                    b"",
                )
            },
        )
    )
    with pytest.raises(ValueError, match="storage base"):
        asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    assert not fake.matching("container", "rm")


@pytest.mark.parametrize(("running", "allowlisted"), [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize("binding", ["different", "missing", "unreadable"])
def test_storage_binding_precedes_lifecycle_changes(running, allowlisted, binding):
    spec = replace(_ALLOW_SPEC if allowlisted else _METHOD_SPEC, work_dir="/other/base")
    name = _AL if allowlisted else _NAME
    labels = {"maf-sandbox.work-dir.v1": _WORK} if binding == "different" else {}
    metadata = [{"Id": f"id-{name}", "Labels": labels, "Config": {"User": ""}}]
    overrides = {
        ("container", "start"): _WslcResult(1, b"", b"start failed"),
        ("container", "inspect"): _WslcResult(
            1 if binding == "unreadable" else 0,
            json.dumps(metadata).encode(),
            b"engine unavailable",
        ),
    }
    backend, fake = _backend_with(
        _machine(
            running=[name] if running else [],
            stopped=[] if running else [name],
            overrides=overrides,
        ),
        config=_ALLOW_CONFIG if allowlisted else None,
    )
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(backend.acquire(_KEY, spec))
    assert all(call.args[1] in {"inspect", "list"} for call in fake.calls)


def test_storage_binding_precedes_adoption_of_a_name_conflict():
    present = False
    spec = replace(_METHOD_SPEC, work_dir="/other/base")
    absent = _machine()
    stopped = _machine(stopped=[_NAME])

    def respond(args):
        nonlocal present
        if args[:2] == ("container", "run"):
            present = True
            return _WslcResult(1, b"", b"ERROR_ALREADY_EXISTS")
        if args[:2] == ("container", "start"):
            return _WslcResult(1, b"", b"start failed")
        if not present and args[:2] == ("container", "inspect"):
            return _WslcResult(1, b"", b"WSLC_E_CONTAINER_NOT_FOUND")
        return (stopped if present else absent)(args)

    backend, fake = _backend_with(respond)
    with pytest.raises(ValueError, match="storage base"):
        asyncio.run(backend.acquire(_KEY, spec))
    assert not any(call.args[1] in {"start", "rm", "remove"} for call in fake.calls)


# ---------------------------------------------------------------------------
# The isolation scope — one sandbox per call, declared and keyed (#436)
# ---------------------------------------------------------------------------

#: What `_container_name` returned for `_KEY` and `_SPEC.kind` before this backend served the
#: call scope, written down rather than recomputed. A conversation-scoped key has to keep
#: mapping to the container it already created, or the release that adds the scope orphans
#: every warm sandbox on the machine and every disposal that derives a name misses it.
_NAME_BEFORE_THE_CALL_SCOPE = "maf-sandbox-wslc-d76deaf0de05"

_CALL_A = replace(_KEY, call_id="call-a")
_CALL_B = replace(_KEY, call_id="call-b")


class TestTheIsolationScope:
    """That a key naming a call is a different container, and is disposed on its own."""

    def test_declares_both_scopes(self):
        scopes = WslcSandboxBackend(WslcSandboxConfig()).declarations.isolation_scopes
        assert scopes == frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL})

    def test_a_conversation_key_maps_to_the_container_it_always_did(self):
        """The upgrade path. Pinned to a literal: recomputing the digest here would agree with
        the implementation whatever either one did, and prove nothing about the release before.
        """
        assert _container_name(_KEY, _SPEC.kind) == _NAME_BEFORE_THE_CALL_SCOPE

    def test_a_key_naming_a_call_is_a_different_container(self):
        assert _container_name(_CALL_A, _SPEC.kind) != _container_name(_KEY, _SPEC.kind)

    def test_two_calls_are_two_containers(self):
        """The property itself, at the level the name decides it: get-or-create resolves each
        call to a name no other call produced, so neither is ever handed the other's warm one.
        """
        assert _container_name(_CALL_A, _SPEC.kind) != _container_name(_CALL_B, _SPEC.kind)

    def test_a_crafted_kind_cannot_spell_the_call_component(self):
        """The parts are joined by `|` with nothing length-prefixing them, so a call appended as
        text would be spellable by a `kind`. `kind="k|call:x"` with no call and `kind="k"` with
        `call_id="x"` would then be one container the backend could neither create nor dispose
        independently, while declaring it serves both.
        """
        forged = _container_name(_KEY, "k|call:x")
        genuine = _container_name(replace(_KEY, call_id="x"), "k")
        assert forged != genuine

    def test_a_call_id_cannot_be_read_as_an_egress_id(self):
        """Both optional parts are appended, so an untagged call id would let a sandbox with an
        allowlist and no call share a name with a call whose id spelled that allowlist.
        """
        egress_only = _container_name(_KEY, _SPEC.kind, "allow:example.com")
        call_only = _container_name(replace(_KEY, call_id="allow:example.com"), _SPEC.kind)
        assert egress_only != call_only

    def test_a_conversation_container_carries_no_call_label(self):
        """Absence is what keeps the label selector reaching containers an earlier release
        created, which carry the four labels this one still writes and nothing more.
        """
        assert "maf-sandbox.call" not in _sandbox_labels(_KEY, _SPEC)

    def test_a_call_scoped_container_is_labelled_with_its_call(self):
        assert _sandbox_labels(_CALL_A, _SPEC)["maf-sandbox.call"] == "call-a"

    def test_the_create_writes_the_call_label(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_CALL_A, _SPEC))
        assert "maf-sandbox.call=call-a" in fake.only("container", "run").args

    def test_a_call_scoped_disposal_selects_on_the_call(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose(_CALL_A, kind=_SPEC.kind))
        listed = [c.args for c in fake.matching("container", "list")]
        assert listed, "the disposal read no listing at all"
        assert all("label=maf-sandbox.call=call-a" in args for args in listed)

    def test_a_conversation_disposal_does_not_filter_on_a_call(self):
        """A conversation's key adds no call filter, so it keeps reaching the containers a
        release before this one labelled with four labels and no fifth.
        """
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose(_KEY, kind=_SPEC.kind))
        listed = [c.args for c in fake.matching("container", "list")]
        assert listed, "the disposal read no listing at all"
        assert not any("maf-sandbox.call" in arg for args in listed for arg in args)

    def test_disposing_one_call_leaves_the_other_calls_registry_entry(self):
        """`assert_call_scope_conformance`'s last probe, at the half this process decides.

        Two calls are two registry entries, so ending one selects one of them. The other half
        — that the engine's own listing returns one container for that filter — is the live
        suite's: the fake here answers `container list` from everything it is holding and reads
        no `--filter label=` at all, so an assertion about which container the *engine* removed
        would pass or fail on the fake rather than on this backend.
        """
        backend, _ = _backend_with(_machine())

        async def scenario() -> None:
            await backend.acquire(_CALL_A, _SPEC)
            await backend.acquire(_CALL_B, _SPEC)
            await backend.dispose(_CALL_A, kind=_SPEC.kind)

        asyncio.run(scenario())
        assert {key[3] for key in backend._registry} == {"call-b"}

    def test_the_purge_selects_on_scope_and_thread_and_not_on_the_call(self):
        """The documented backstop: a per-call delete that does not land leaves a container no
        later call can address, and the purge's filter is what still reaches it.
        """
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        listed = [c.args for c in fake.matching("container", "list")]
        assert listed, "the purge read no listing at all"
        assert not any("maf-sandbox.call" in arg for args in listed for arg in args)
