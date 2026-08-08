"""Offline tests for the wslc backend.

No WSL and no container: the one seam every ``wslc`` invocation goes through is replaced by
a fake that records argv and replays canned results, so what these tests pin is the command
line this backend actually builds.  Two tests reach the real seam anyway — with
``sys.executable`` standing in for ``wslc.exe`` — because the subprocess handling itself
(decoding, exit codes, killing on timeout) is the one part a fake cannot prove.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import tarfile
from collections.abc import Sequence

import pytest
from maf_sandbox import Egress, ExecResult, Isolation, SandboxBackend, SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _container_name, _WslcResult

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY)


class _Recorded:
    def __init__(self, args: tuple[str, ...], stdin: bytes | None, timeout: float | None) -> None:
        self.args = args
        self.stdin = stdin
        self.timeout = timeout


class _FakeWslc:
    """Stands in for `WslcSandboxBackend._wslc`."""

    def __init__(self, responder=None) -> None:
        self.calls: list[_Recorded] = []
        self._responder = responder or (lambda args: _WslcResult(0, "", ""))

    async def __call__(self, *args: str, stdin=None, timeout=None) -> _WslcResult:
        self.calls.append(_Recorded(args, stdin, timeout))
        return self._responder(args)

    def matching(self, *prefix: str) -> list[_Recorded]:
        return [c for c in self.calls if c.args[: len(prefix)] == prefix]

    def only(self, *prefix: str) -> _Recorded:
        found = self.matching(*prefix)
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
                return _WslcResult(0, json.dumps(payload), "")
            return _WslcResult(0, "".join(f"id-{n}\n" for n in names), "")
        return _WslcResult(0, "", "")

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
# Backend identity — read by the router's deployed check
# ---------------------------------------------------------------------------


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(WslcSandboxBackend(WslcSandboxConfig()), SandboxBackend)

    def test_declares_container_isolation(self):
        """`deployed=True` refuses this backend because of this value, by design."""
        assert WslcSandboxBackend(WslcSandboxConfig()).isolation == Isolation.CONTAINER

    def test_declares_closed_egress(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).egress == Egress.CLOSED

    def test_is_named_wslc(self):
        assert WslcSandboxBackend(WslcSandboxConfig()).name == "wslc"


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

    def test_the_name_is_derived_from_the_key_alone(self):
        assert _container_name(_KEY) == _container_name(
            SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        )
        assert _container_name(_KEY) != _container_name(
            SandboxKey(scope="scope-b", thread_id="thread-1", agent_dir="devops-engineer")
        )

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
        overrides = {("container", "start"): _WslcResult(1, "", "WSLC_E_CONTAINER_CORRUPT")}
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
        asyncio.run(sandbox.exec(argv, working_directory="/acas/work", timeout=5))

        args = fake.only("container", "exec").args
        assert args == ("container", "exec", "-w", "/acas/work", _NAME, *argv)
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
        overrides = {("container", "exec"): _WslcResult(7, "out\n", "err\n")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        result = asyncio.run(sandbox.exec(["false"], working_directory="/w", timeout=5))

        assert result == ExecResult(stdout="out\n", stderr="err\n", exit_code=7)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def _sent(self, path: str, content: str) -> tuple[_Recorded, tarfile.TarFile]:
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(path, content))
        call = fake.only("container", "cp")
        assert call.stdin is not None
        return call, tarfile.open(fileobj=io.BytesIO(call.stdin), mode="r")

    def test_the_copy_targets_the_container_root(self):
        """A `cp` destination must already exist, and `/` is the only path that always does."""
        call, _ = self._sent("/acas/work/main.bicep", "x")
        assert call.args == ("container", "cp", "-", f"{_NAME}:/")

    def test_the_entry_is_the_path_without_its_leading_slash(self):
        _, archive = self._sent("/acas/work/r1/main.bicep", "x")
        assert archive.getnames() == ["acas/work/r1/main.bicep"]

    def test_a_relative_path_is_left_alone(self):
        _, archive = self._sent("acas/work/main.bicep", "x")
        assert archive.getnames() == ["acas/work/main.bicep"]

    def test_the_content_round_trips_as_utf8(self):
        _, archive = self._sent("/acas/work/main.bicep", "param naïve string\n")
        member = archive.extractfile("acas/work/main.bicep")
        assert member is not None
        assert member.read().decode("utf-8") == "param naïve string\n"

    def test_the_entry_is_readable(self):
        _, archive = self._sent("/acas/work/main.bicep", "x")
        assert archive.getmember("acas/work/main.bicep").mode == 0o644

    def test_a_failed_copy_raises(self):
        """A write that silently did nothing would surface as a compiler error about a file
        the workload believes it just wrote."""
        overrides = {("container", "cp"): _WslcResult(1, "", "WSLC_E_PATH_NOT_FOUND")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))

        with pytest.raises(RuntimeError, match="WSLC_E_PATH_NOT_FOUND"):
            asyncio.run(sandbox.write_file("/acas/work/main.bicep", "x"))


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
        not_found = _WslcResult(1, "", "Error code: WSLC_E_CONTAINER_NOT_FOUND\n")
        backend, _ = _backend_with(_machine(overrides={("container", "remove"): not_found}))

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_wslc"):
            asyncio.run(backend.dispose(_KEY))
        assert caplog.records == []

    def test_never_raises(self):
        backend, _ = _backend_with(_explodes)
        asyncio.run(backend.dispose(_KEY))


class TestDisposeScope:
    def test_selects_on_both_labels_and_on_stopped_containers_too(self):
        backend, fake = _backend_with(_machine(stopped=["a", "b"]))
        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        args = fake.only("container", "list").args
        assert args[:4] == ("container", "list", "-a", "-q")
        assert args[4:] == (
            "--filter",
            "label=maf-sandbox.scope=scope-a",
            "--filter",
            "label=maf-sandbox.thread=thread-1",
        )

    def test_removes_every_listed_id_and_returns_the_count(self):
        backend, fake = _backend_with(_machine(stopped=["a", "b"]))

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 2
        assert [c.args[-1] for c in fake.matching("container", "remove")] == ["id-a", "id-b"]

    def test_nothing_to_purge_is_zero_not_an_error(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0

    def test_a_container_this_process_created_survives_a_failing_listing(self):
        """The labels are the source of truth; the registry is what is left when they fail."""
        overrides = {("container", "list"): _WslcResult(1, "", "WSLC_E_SERVICE_UNAVAILABLE")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        backend._registry[("scope-a", "thread-1", "devops")] = "name-x"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 1
        assert fake.only("container", "remove").args[-1] == "name-x"

    def test_another_scopes_container_is_left_alone(self):
        backend, fake = _backend_with(_machine())
        backend._registry[("scope-b", "thread-1", "devops")] = "name-other"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0
        assert fake.matching("container", "remove") == []
        assert ("scope-b", "thread-1", "devops") in backend._registry

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
        queried = fake2.only("container", "list").args[5]

        assert queried == f"label={written}"


# ---------------------------------------------------------------------------
# The seam itself — the part a fake cannot prove
# ---------------------------------------------------------------------------


class TestTheSeam:
    """`sys.executable` stands in for `wslc.exe`: same subprocess handling, no WSL needed."""

    def test_stdout_stderr_and_exit_code_come_back_decoded(self):
        """`wslc` pipes a container's own bytes through untouched, and they are UTF-8 — so the
        child here writes bytes rather than text, which is what the seam actually receives."""
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = (
            "import sys; sys.stdout.buffer.write('naïve'.encode()); "
            "sys.stderr.buffer.write('ünï'.encode()); sys.exit(3)"
        )
        result = asyncio.run(backend._wslc("-c", script, timeout=30))

        assert (result.returncode, result.stdout, result.stderr) == (3, "naïve", "ünï")

    def test_stdin_reaches_the_process(self):
        backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
        script = "import sys; sys.stdout.write(sys.stdin.buffer.read().decode())"
        result = asyncio.run(backend._wslc("-c", script, stdin=b"tar bytes", timeout=30))

        assert result.stdout == "tar bytes"

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
