"""Offline tests for the Bicep sandbox workload (issue #408).

The whole workload runs here against a **fake backend** — create, write, exec, parse — with
no Azure, no Bicep binary and no host application.  That end-to-end path had no test at all
until the router seam existed (issue #663 called this out as its own acceptance criterion),
so its absence is what several review rounds were spent compensating for by reading.

Also covered: the path-safety guard, SARIF parsing, the command templates, and the fact that
this package imports nothing from the application that hosts it.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from maf_aca_sandboxes.bicep import (
    BICEP_TOOL_NAMES,
    BICEP_VALIDATE_TOOL_NAME,
    bicep_sandbox_spec,
    format_diagnostics,
    make_bicep_tools,
    parse_sarif,
    safe_workspace_path,
)
from maf_aca_sandboxes.bicep._tool import _BUILD_CMD, _LINT_CMD
from sandbox_router import ExecResult, Isolation, SandboxRouter, WorkspaceContext

# ---------------------------------------------------------------------------
# Fakes: a backend that records what the workload asked it to do
# ---------------------------------------------------------------------------


def _sarif(rule: str = "no-unused-params", message: str = "Parameter 'foo' is unused.") -> str:
    return json.dumps(
        {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "bicep", "rules": [{"id": rule, "helpUri": "u"}]}},
                    "results": [
                        {
                            "ruleId": rule,
                            "level": "error",
                            "message": {"text": message},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "main.bicep"},
                                        "region": {"startLine": 5, "startColumn": 7},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


_EMPTY_SARIF = json.dumps({"version": "2.1.0", "runs": []})


class _FakeSandbox:
    def __init__(self, outputs: dict[str, str] | None = None, raises: BaseException | None = None):
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, str, float]] = []
        self._outputs = outputs or {}
        self._raises = raises

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def exec(self, command: str, *, working_directory: str, timeout: float) -> ExecResult:
        self.commands.append((command, working_directory, timeout))
        if self._raises is not None:
            raise self._raises
        for marker, output in self._outputs.items():
            if marker in command:
                return ExecResult(stdout=output)
        return ExecResult(stdout=_EMPTY_SARIF)


class _FakeBackend:
    """An in-process backend — issue #663's `fake`, in the shape the protocol asks for."""

    def __init__(self, sandbox: _FakeSandbox | None = None, acquire_error=None) -> None:
        self.sandbox = sandbox or _FakeSandbox()
        self.acquire_error = acquire_error
        self.specs: list = []
        self.keys: list = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def isolation(self) -> str:
        return Isolation.PROCESS

    async def acquire(self, key, spec):
        if self.acquire_error is not None:
            raise self.acquire_error
        self.keys.append(key)
        self.specs.append(spec)
        return self.sandbox

    async def dispose(self, key) -> None:
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        return 0


class _FakeStore:
    """The slice of AgentFileStore the workload uses."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)

    async def read(self, name: str) -> str:
        return self.files[name]


def _context(store: _FakeStore, *, thread_id: str | None = "thread-1") -> WorkspaceContext:
    async def _list(_s) -> list[str]:
        return list(store.files)

    return WorkspaceContext(
        current_scope=lambda: "scope-a",
        current_thread_id=lambda: thread_id,
        list_files=_list,
    )


def _tool(store: _FakeStore, backend: _FakeBackend, *, thread_id: str | None = "thread-1", **kw):
    tools = make_bicep_tools(
        SandboxRouter([backend]),
        store,
        "devops-engineer",
        _context(store, thread_id=thread_id),
        image="acr.io/bicep:1",
        **kw,
    )
    assert len(tools) == 1
    return tools[0]


def _workspace_part(sandbox_path: str) -> str:
    """Strip `<work dir>/<per-call dir>/` off a sandbox path, leaving the workspace path."""
    from maf_aca_sandboxes.bicep._tool import _WORK_DIR

    return sandbox_path.removeprefix(f"{_WORK_DIR}/").split("/", 1)[1]


def _run(tool, files: list[str]) -> str:
    """Invoke the MAF-decorated tool, whichever attribute carries the callable."""
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
    return asyncio.run(fn(files=files))


# ---------------------------------------------------------------------------
# End to end against the fake backend — the path that had no test
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_writes_the_file_into_the_sandbox_and_reports_clean(self):
        store = _FakeStore({"main.bicep": "param unused string"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), ["main.bicep"])

        assert list(backend.sandbox.files.values()) == ["param unused string"]
        (path,) = backend.sandbox.files
        assert path.endswith("/main.bicep")
        assert "build(main.bicep): no diagnostics" in out
        assert "lint(main.bicep): no diagnostics" in out

    def test_runs_build_then_lint_with_the_fixed_templates(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        (path,) = backend.sandbox.files
        commands = [c for c, _, _ in backend.sandbox.commands]
        assert commands == [_BUILD_CMD.format(path=path), _LINT_CMD.format(path=path)]

    def test_renders_diagnostics_from_sarif(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend(_FakeSandbox(outputs={"bicep lint": _sarif()}))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "lint(main.bicep): 1 diagnostic(s)" in out
        assert "[error] no-unused-params @ main.bicep:5:7" in out

    def test_unparseable_output_is_an_error_not_a_clean_build(self):
        """A broken sandbox must never read as "no diagnostics"."""
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend(_FakeSandbox(outputs={"bicep build": "Segmentation fault"}))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "build(main.bicep): Error: could not parse SARIF output" in out

    def test_the_exec_timeout_is_passed_through(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend, exec_timeout_seconds=7), ["main.bicep"])

        assert {t for _, _, t in backend.sandbox.commands} == {7}

    def test_a_timeout_is_reported_per_phase_rather_than_hanging(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend(_FakeSandbox(raises=TimeoutError()))
        out = _run(_tool(store, backend, exec_timeout_seconds=3), ["main.bicep"])

        assert "build(main.bicep): Error: timed out after 3s" in out
        assert "lint(main.bicep): Error: timed out after 3s" in out

    def test_validates_every_file_it_is_given(self):
        store = _FakeStore({"a.bicep": "1", "b.bicep": "2"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), ["a.bicep", "b.bicep"])

        # Paths are <work dir>/<per-call dir>/<workspace path>; assert the last part rather
        # than pinning a directory that changes every call (see TestStaleFilesAcrossRounds).
        assert {_workspace_part(p) for p in backend.sandbox.files} == {"a.bicep", "b.bicep"}
        assert out.count("no diagnostics") == 4  # build + lint, per file

    def test_the_key_carries_the_hosts_scope_and_thread_not_model_input(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        key = backend.keys[0]
        assert (key.scope, key.thread_id, key.agent_dir) == (
            "scope-a",
            "thread-1",
            "devops-engineer",
        )

    def test_the_spec_it_asks_for_allows_only_mcr(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        assert backend.specs[0].egress_allow == ("mcr.microsoft.com",)
        assert backend.specs[0].image == "acr.io/bicep:1"


class TestStaleFilesAcrossRounds:
    """A reused sandbox must not let last round's files influence this round's build.

    The sandbox is keyed per `(scope, thread, agent)` and reused across fix rounds, but only
    the *named* files are written into it. Delete a file from the workspace between rounds
    while a template still references it and, without isolation, the stale copy on the
    sandbox disk makes `bicep build` succeed — the tool reports "no diagnostics" for
    something that cannot build from the actual workspace. A false green from the one tool
    whose entire purpose is compiler truth.
    """

    def test_each_call_writes_into_a_fresh_directory(self):
        store = _FakeStore({"main.bicep": "x", "modules/storage.bicep": "y"})
        backend = _FakeBackend()
        tool = _tool(store, backend)

        _run(tool, ["main.bicep", "modules/storage.bicep"])
        first = set(backend.sandbox.files)

        # Round two: the module is gone from the workspace and is not named.
        store.files.pop("modules/storage.bicep")
        _run(tool, ["main.bicep"])
        second = set(backend.sandbox.files) - first

        assert len(second) == 1
        (round_two_path,) = second
        assert round_two_path.endswith("/main.bicep")
        # The two rounds share no directory, so nothing from the first is reachable by a
        # relative reference resolved from the second.
        assert {p.rsplit("/", 1)[0] for p in first}.isdisjoint({round_two_path.rsplit("/", 1)[0]})

    def test_the_stale_module_is_not_in_the_second_rounds_directory(self):
        store = _FakeStore({"main.bicep": "x", "modules/storage.bicep": "y"})
        backend = _FakeBackend()
        tool = _tool(store, backend)

        _run(tool, ["main.bicep", "modules/storage.bicep"])
        store.files.pop("modules/storage.bicep")
        _run(tool, ["main.bicep"])

        # Whatever directory round two compiled in, the deleted module is not under it.
        build_cmd = [c for c, _, _ in backend.sandbox.commands if "bicep build" in c][-1]
        round_dir = build_cmd.split("bicep build ")[1].split("/main.bicep")[0]
        assert f"{round_dir}/modules/storage.bicep" not in backend.sandbox.files

    def test_the_round_directory_sits_under_the_work_dir(self):
        """`bicepconfig.json` is at the work-dir root; Bicep finds it by walking up."""
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        from maf_aca_sandboxes.bicep._tool import _WORK_DIR

        (path,) = backend.sandbox.files
        assert path.startswith(f"{_WORK_DIR}/")
        # Not the root itself — that is where bicepconfig.json lives.
        assert path != f"{_WORK_DIR}/main.bicep"

    def test_the_compiler_runs_in_the_round_directory(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        (path,) = backend.sandbox.files
        round_dir = path.rsplit("/", 1)[0]
        assert {wd for _, wd, _ in backend.sandbox.commands} == {round_dir}


class TestConfigDiscovery:
    """The image must ship `bicepconfig.json` at the root the tool writes under.

    Bicep resolves that file only by walking up from the source, and the pinned CLI has no
    `--config-file` flag on `build` or `lint`. So if the image's path and `_WORK_DIR` ever
    drift apart, the config is simply never found and `bicep lint` falls back to its
    built-in defaults — while still returning parseable SARIF and rendering diagnostics
    normally. Nothing else in this suite would notice, which is the whole reason this test
    reaches outside the package to read the Dockerfile.
    """

    def _dockerfile(self):
        import pathlib

        import maf_aca_sandboxes

        distribution = pathlib.Path(maf_aca_sandboxes.__file__).parents[2]
        candidates = [
            # In the host repo, where images/ is a sibling of src/.
            distribution.parents[1] / "images" / "bicep-sandbox" / "Dockerfile",
            # After extraction, where images/ comes along as a sibling of src/.
            distribution / "images" / "bicep-sandbox" / "Dockerfile",
        ]
        for path in candidates:
            if path.is_file():
                return path
        raise AssertionError(
            f"bicep-sandbox Dockerfile not found at any of {[str(p) for p in candidates]}. "
            "If this package moved, update the candidates rather than deleting the test — "
            "it guards a silent lint-rule downgrade."
        )

    def test_the_image_puts_bicepconfig_at_the_work_dir_root(self):
        from maf_aca_sandboxes.bicep._tool import _WORK_DIR

        text = self._dockerfile().read_text(encoding="utf-8")
        assert f"COPY bicepconfig.json {_WORK_DIR}/bicepconfig.json" in text, (
            f"the image must COPY bicepconfig.json to {_WORK_DIR}/, the root the tool writes "
            "each validation under — Bicep finds it only by walking up from the source file"
        )

    def test_the_round_directory_is_a_child_of_that_root(self):
        """One level down, so the walk-up reaches the config in a single step."""
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        _run(_tool(store, backend), ["main.bicep"])

        from maf_aca_sandboxes.bicep._tool import _WORK_DIR

        (path,) = backend.sandbox.files
        assert path.startswith(f"{_WORK_DIR}/")
        assert path.count("/") == _WORK_DIR.count("/") + 2  # <root>/<round>/main.bicep


class TestEndToEndRefusals:
    def test_rejects_a_non_bicep_extension_before_touching_the_sandbox(self):
        store = _FakeStore({"main.tf": "x"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), ["main.tf"])

        assert "only accepts .bicep and .bicepparam" in out
        assert backend.keys == []

    def test_rejects_a_file_that_is_not_in_the_workspace_listing(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), ["other.bicep"])

        assert "not in the workspace listing" in out
        assert backend.sandbox.files == {}

    def test_rejects_an_injection_attempt_that_is_really_in_the_workspace(self):
        """Being in the listing is not evidence a name is safe to interpolate.

        The name has to end in `.bicep` to get this far: the extension check runs first, so
        the obvious `main.bicep; rm -rf /` never reaches the path guard at all. That is
        defence in depth working, and it is why this test uses a payload that survives the
        first gate — otherwise it would pass without ever exercising the second.
        """
        malicious = "a;$(id).bicep"
        store = _FakeStore({malicious: "x"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), [malicious])

        assert "unsafe characters" in out
        assert backend.sandbox.commands == []

    def test_the_extension_gate_runs_before_the_path_guard(self):
        """Pins the ordering the test above depends on."""
        store = _FakeStore({"main.bicep; rm -rf /": "x"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend), ["main.bicep; rm -rf /"])

        assert "only accepts .bicep and .bicepparam" in out
        assert backend.sandbox.commands == []

    def test_no_thread_context_is_refused(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend()
        out = _run(_tool(store, backend, thread_id=None), ["main.bicep"])

        assert "no active thread context" in out

    def test_an_unavailable_sandbox_degrades_to_t0_without_leaking_sdk_detail(self):
        """SDK errors carry endpoint/subscription/tenant and tool results are persisted."""
        store = _FakeStore({"main.bicep": "x"})
        secret = "https://management.eastus.azuredevcompute.io subscription 0000-1111"
        backend = _FakeBackend(acquire_error=RuntimeError(secret))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "degrading to T0" in out
        assert "azuredevcompute" not in out
        assert "0000-1111" not in out

    def test_a_configuration_error_is_surfaced_because_we_authored_it(self):
        store = _FakeStore({"main.bicep": "x"})
        backend = _FakeBackend(acquire_error=ValueError("No disk image ... was built from 'x'"))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "No disk image" in out


# ---------------------------------------------------------------------------
# Attach / do not attach
# ---------------------------------------------------------------------------


class TestMakeBicepTools:
    """A host with no sandbox gets no tool, not a tool that fails when called."""

    def test_returns_empty_without_a_router(self):
        assert (
            make_bicep_tools(None, _FakeStore({}), "devops-engineer", _context(_FakeStore({})))
            == []
        )

    def test_returns_empty_when_the_router_has_no_backend(self):
        store = _FakeStore({})
        router = SandboxRouter([])
        assert make_bicep_tools(router, store, "devops-engineer", _context(store)) == []

    def test_tool_has_correct_name(self):
        store = _FakeStore({})
        tool = _tool(store, _FakeBackend())
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__tool_definition__", None), "name", None
        )
        assert name == BICEP_VALIDATE_TOOL_NAME

    def test_tool_names_table_matches_the_tool(self):
        assert BICEP_TOOL_NAMES == frozenset({BICEP_VALIDATE_TOOL_NAME})


# ---------------------------------------------------------------------------
# The spec — containment that must not be configurable away
# ---------------------------------------------------------------------------


class TestBicepSandboxSpec:
    def test_allows_exactly_one_host(self):
        assert bicep_sandbox_spec().egress_allow == ("mcr.microsoft.com",)

    def test_work_dir_is_a_dedicated_root(self):
        """Everything shared with the sandbox lives here, on a path nothing else owns."""
        assert bicep_sandbox_spec().work_dir == "/acas/work"

    def test_kind_is_bicep(self):
        assert bicep_sandbox_spec().kind == "bicep"


# ---------------------------------------------------------------------------
# Command templates — pinned because deleting the redirection is silent
# ---------------------------------------------------------------------------


class TestCommandTemplates:
    """The build/lint command strings carry behaviour no other test would catch.

    `bicep build` writes SARIF to **stderr** while `bicep lint` writes it to **stdout**, and
    both legs read `.stdout`.  Dropping `2>&1` from the build template therefore makes every
    build report "could not parse SARIF output" against an otherwise healthy sandbox — a
    regression that shipped once already and was caught by hand, not by CI.
    """

    def test_build_merges_stderr_into_stdout(self):
        assert "2>&1" in _BUILD_CMD, (
            "bicep build emits SARIF on stderr; without 2>&1 the parser sees an empty stdout"
        )

    def test_both_templates_request_sarif(self):
        assert "--diagnostics-format sarif" in _BUILD_CMD
        assert "--diagnostics-format sarif" in _LINT_CMD

    def test_path_is_the_only_interpolation(self):
        """Anything else in the template would be agent-influenced text in a shell string."""
        import re

        for template in (_BUILD_CMD, _LINT_CMD):
            assert re.findall(r"\{(\w+)\}", template) == ["path"]


# ---------------------------------------------------------------------------
# safe_workspace_path — injection-pinning guard
# ---------------------------------------------------------------------------


class TestSafeWorkspacePath:
    def test_returns_sandbox_path_for_valid_file_in_listing(self):
        assert safe_workspace_path("main.bicep", ["main.bicep"], "/work") == "/work/main.bicep"

    def test_normalises_leading_slash(self):
        assert safe_workspace_path("/main.bicep", ["main.bicep"], "/work") == "/work/main.bicep"

    def test_normalises_dot_slash(self):
        assert safe_workspace_path("./main.bicep", ["main.bicep"], "/work") == "/work/main.bicep"

    def test_accepts_subpath(self):
        result = safe_workspace_path("infra/main.bicep", ["infra/main.bicep"], "/work")
        assert result == "/work/infra/main.bicep"

    def test_rejects_file_not_in_listing(self):
        assert safe_workspace_path("other.bicep", ["main.bicep"], "/work") is None

    def test_rejects_empty_name(self):
        assert safe_workspace_path("", ["main.bicep", ""], "/work") is None

    @pytest.mark.parametrize(
        "malicious",
        [
            "main.bicep; rm -rf /",
            "main.bicep`id`",
            "${PATH}",
            "main.bicep|cat /etc/passwd",
            "main bicep",
            "main.bicep\nbicep build /etc/passwd",
        ],
    )
    def test_rejects_shell_metacharacters_even_when_present_in_the_listing(self, malicious):
        assert safe_workspace_path(malicious, [malicious], "/work") is None

    @pytest.mark.parametrize("traversal", ["../../etc/passwd", "infra/../../../etc/passwd"])
    def test_rejects_parent_traversal(self, traversal):
        assert safe_workspace_path(traversal, [traversal], "/work") is None


# ---------------------------------------------------------------------------
# SARIF
# ---------------------------------------------------------------------------


class TestParseSarif:
    def test_returns_none_for_empty_string(self):
        assert parse_sarif("") is None

    def test_returns_none_for_non_json(self):
        assert parse_sarif("not json") is None

    def test_returns_empty_for_no_runs(self):
        assert parse_sarif(json.dumps({"version": "2.1.0"})) == []

    def test_parses_single_diagnostic(self):
        diags = parse_sarif(_sarif())
        assert diags is not None and len(diags) == 1
        d = diags[0]
        assert d["rule"] == "no-unused-params"
        assert d["level"] == "error"
        assert "foo" in d["message"]
        assert d["locations"][0] == {"file": "main.bicep", "line": 5, "column": 7}

    def test_returns_empty_for_zero_results(self):
        assert parse_sarif(_EMPTY_SARIF) == []


class TestAgainstRealBicepOutput:
    """The fixture is genuine output from the pinned CLI in the shipped image, not written.

    Every other SARIF test here uses a hand-made blob, and hand-made blobs agree with the
    code that reads them. Running the real binary produced two things no fixture of mine
    had: locations as absolute ``file://`` URIs, and ``charOffset`` where the parser looks
    for ``startColumn``. Both reached the model — as a leaked internal path that changed
    every round, and as a literal ``:None`` — and neither was visible until the real thing
    ran. Regenerate with:

        bicep lint <file> --diagnostics-format sarif
    """

    def _real(self) -> str:
        import pathlib

        fixture = pathlib.Path(__file__).parent / "fixtures" / "bicep-lint-real.sarif.json"
        return fixture.read_text(encoding="utf-8")

    def test_parses_real_output(self):
        diagnostics = parse_sarif(self._real())
        assert diagnostics is not None
        assert len(diagnostics) == 3
        assert {d["rule"] for d in diagnostics} == {"no-unused-params", "no-unused-vars"}

    def test_the_repo_rule_set_was_applied(self):
        """`no-unused-params` is `error` in the repo config and a warning by default.

        Bicep omits `level` entirely when it is the default, so an `error` here is proof the
        config at the work-dir root was found by walking up from a per-call subdirectory.
        """
        diagnostics = parse_sarif(self._real()) or []
        assert {d["level"] for d in diagnostics} == {"error"}

    def test_rendering_strips_the_sandbox_path(self):
        """The model should see the name it asked about, not the sandbox's internals."""
        out = format_diagnostics(
            parse_sarif(self._real()) or [], "lint(main.bicep)", strip_prefix="/acas/work/r2"
        )

        assert "main.bicep:1" in out
        assert "/acas/work" not in out
        assert "file://" not in out

    def test_rendering_omits_a_column_bicep_does_not_provide(self):
        """Real Bicep emits charOffset, not startColumn — so print the line, not ':None'."""
        out = format_diagnostics(parse_sarif(self._real()) or [], "lint(x)", strip_prefix=None)

        assert "None" not in out


class TestFormatDiagnostics:
    def test_no_diagnostics_reads_clean(self):
        assert format_diagnostics([], "build(x)") == "build(x): no diagnostics"

    def test_renders_location_and_rule(self):
        out = format_diagnostics(parse_sarif(_sarif()) or [], "lint(x)")
        assert "lint(x): 1 diagnostic(s)" in out
        assert "no-unused-params @ main.bicep:5:7" in out


# ---------------------------------------------------------------------------
# Independence from the host application — the invariant the split exists for
# ---------------------------------------------------------------------------

#: The one place these distributions name the application they currently ship inside.  It
#: is here because the guard below needs something to look for; everywhere else the host is
#: referred to by role, so moving this tree to its own repository is a file move plus this
#: single line.
_HOST_PACKAGE = "ats"


class TestNoHostDependency:
    """These packages must not import the application they currently ship inside.

    Everything else here would keep passing if someone added ``from <host>.config import
    Settings`` to a module — the tests run in a process where the host package is
    importable, so the coupling would be invisible until the day someone tried to extract
    the package.  A source scan suffices: the only imports here are stdlib,
    ``agent_framework``, ``sandbox_router`` and ``azure.*``, so the host cannot arrive
    transitively.
    """

    def _sources(self):
        import pathlib

        import maf_aca_sandboxes
        import sandbox_router

        paths = []
        for module in (maf_aca_sandboxes, sandbox_router):
            root = pathlib.Path(module.__file__).parent  # type: ignore[arg-type]
            distribution = root.parent.parent
            for directory in (root, distribution / "tests", distribution / "scripts"):
                paths.extend(directory.rglob("*.py"))
        return paths

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(self._sources()) >= 8

    def test_nothing_imports_the_host_application(self):
        import re

        host = re.escape(_HOST_PACKAGE)
        pattern = re.compile(rf"(?m)^\s*(?:from\s+{host}[.\s]|import\s+{host}[.\s])")
        offenders = [
            str(p) for p in self._sources() if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"these files import the host application ({_HOST_PACKAGE!r}): {offenders}. "
            "The dependency belongs in the host's own adapter module, reaching these "
            "packages through SandboxConfig / WorkspaceContext."
        )

    def test_the_workload_does_not_import_azure(self):
        """Issue #663's acceptance criterion: the same tool must run on any backend."""
        import pathlib
        import re

        import maf_aca_sandboxes.bicep as bicep_pkg

        root = pathlib.Path(bicep_pkg.__file__).parent  # type: ignore[arg-type]
        pattern = re.compile(r"(?m)^\s*(?:from\s+azure[.\s]|import\s+azure[.\s])")
        offenders = [
            str(p) for p in root.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"the bicep workload imports Azure directly: {offenders}. "
            "It must reach a sandbox through sandbox_router, or it stops being portable."
        )
