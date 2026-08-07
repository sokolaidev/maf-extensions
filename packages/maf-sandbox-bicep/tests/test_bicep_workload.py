"""Offline tests for the Bicep sandbox workload.

The whole workload runs here against a **fake backend** — create, write, exec, parse — with
no Azure, no Bicep binary and no host application.  That end-to-end path had no test at all
until the router seam existed, and covering it was called out as its own acceptance
criterion, so its absence is what several review rounds were spent compensating for by
reading.

Also covered: the path-safety guard, SARIF parsing, the command templates, and the fact that
this package imports nothing from the application that hosts it.

The fakes themselves live in :mod:`maf_sandbox.testing` rather than here — this module
supplies only what is Bicep-specific: the empty-SARIF default and the per-test recording
subclass.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from maf_sandbox import SandboxRouter, WorkspaceContext
from maf_sandbox.testing import InMemoryStore, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_bicep import (
    BICEP_TOOL_NAMES,
    BICEP_VALIDATE_TOOL_NAME,
    bicep_sandbox_spec,
    format_diagnostics,
    make_bicep_tools,
    parse_sarif,
    safe_workspace_path,
)
from maf_sandbox_bicep._tool import _BUILD_CMD, _LINT_CMD

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


def _fake_backend(
    sandbox: InProcessSandbox | None = None, acquire_error: BaseException | None = None
) -> InProcessSandboxBackend:
    """A backend whose sandbox defaults to returning an empty-but-valid SARIF document.

    :class:`~maf_sandbox.testing.InProcessSandbox` leaves what an unmatched command's stdout
    is to the caller — deliberately, so no generic fake bakes in one kind's output shape.
    Every test in this module wants an empty SARIF document there: a bare empty string fails
    to parse and reads as a broken sandbox rather than a clean run, and that distinction is
    exactly what :class:`TestExecutionIsVisible` depends on.
    """
    return InProcessSandboxBackend(
        sandbox if sandbox is not None else InProcessSandbox(default_stdout=_EMPTY_SARIF),
        acquire_error=acquire_error,
    )


def _context(store: InMemoryStore, *, thread_id: str | None = "thread-1") -> WorkspaceContext:
    return WorkspaceContext(
        current_scope=lambda: "scope-a",
        current_thread_id=lambda: thread_id,
        list_files=InMemoryStore.list,
    )


def _tool(
    store: InMemoryStore,
    backend: InProcessSandboxBackend,
    *,
    thread_id: str | None = "thread-1",
    **kw,
):
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
    from maf_sandbox_bicep._tool import _WORK_DIR

    return sandbox_path.removeprefix(f"{_WORK_DIR}/").split("/", 1)[1]


def _run(tool, files: list[str]) -> str:
    """Invoke the MAF-decorated tool, whichever attribute carries the callable."""
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
    return asyncio.run(fn(files=files))


# ---------------------------------------------------------------------------
# End to end against the fake backend — the path that had no test
# ---------------------------------------------------------------------------


class TestToolDescription:
    """The description is the only instruction the model reliably reads at call time.

    Host prompts can be edited, truncated or replaced; the tool's own docstring travels with
    the tool. Since passing one file at a time produces `BCP091 … could not find a part of
    the path` for files that exist, "send the whole set" belongs here rather than only in a
    skill file.
    """

    def test_it_tells_the_caller_to_send_the_whole_set(self):
        store = InMemoryStore({"main.bicep": "x"})
        tool = _tool(store, _fake_backend())
        fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        doc = (fn.__doc__ or "").lower()

        assert "subfolder" in doc, "the description must mention modules in subfolders"
        assert "one call" in doc, "the description must ask for a single call"
        assert "bcp091" in doc, (
            "the description should name the diagnostic a partial call produces — it is what "
            "makes the instruction concrete rather than advisory"
        )


class TestWriteOrdering:
    """Every file must be in the sandbox before any of them is compiled.

    Bicep resolves `module '…/db.bicep'` and a parameter file's `using '…'` off the
    filesystem at compile time. Writing and compiling one file at a time builds the first
    file against a sandbox where its siblings do not exist, producing "module not found"
    diagnostics that are artefacts of the loop rather than defects in the IaC — and for a
    `.bicepparam`, which always references a template, that is wrong whenever the model
    happens to list it first.
    """

    def _recording_sandbox(self, events: list[tuple[str, str]]) -> InProcessSandbox:
        class _Recording(InProcessSandbox):
            async def write_file(self, path: str, content: str) -> None:
                events.append(("write", path))
                await super().write_file(path, content)

            async def exec(self, command: str, *, working_directory: str, timeout: float):
                events.append(("exec", command))
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        return _Recording(default_stdout=_EMPTY_SARIF)

    def test_all_writes_precede_all_execs(self):
        events: list[tuple[str, str]] = []
        store = InMemoryStore(
            {
                "main.bicep": "module db 'modules/db.bicep' = {}",
                "modules/db.bicep": "param name string",
            }
        )
        backend = _fake_backend(sandbox=self._recording_sandbox(events))

        _run(_tool(store, backend), ["main.bicep", "modules/db.bicep"])

        kinds = [kind for kind, _ in events]
        assert kinds == ["write", "write"] + ["exec"] * 4, events
        assert kinds.index("exec") == 2, (
            f"a file was compiled before every file had been written: {events}"
        )

    def test_a_parameter_file_listed_first_still_sees_its_template(self):
        events: list[tuple[str, str]] = []
        store = InMemoryStore(
            {"main.bicepparam": "using 'main.bicep'", "main.bicep": "param x string"}
        )
        backend = _fake_backend(sandbox=self._recording_sandbox(events))

        # The parameter file first — the order that used to compile it against a sandbox
        # holding nothing but itself.
        _run(_tool(store, backend), ["main.bicepparam", "main.bicep"])

        written_before_first_exec = [
            path for kind, path in events[: [k for k, _ in events].index("exec")] if kind == "write"
        ]
        assert len(written_before_first_exec) == 2, events
        assert any(p.endswith("main.bicep") for p in written_before_first_exec), events


class TestParameterFiles:
    """`.bicepparam` needs `build-params`, not `build`.

    `bicep build` refuses a parameter file outright — "was not recognized as a Bicep file"
    — and that sentence is not SARIF, so the phase died in the parser and the tool reported
    "could not parse SARIF output" for an extension it advertises as supported. Verified
    against the pinned CLI in the image: `build` rejects it, `build-params` and `lint` both
    return valid SARIF, and both write it to stderr.
    """

    def test_a_bicepparam_is_built_with_build_params(self):
        store = InMemoryStore({"main.bicepparam": "using 'main.bicep'"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicepparam"])

        commands = [c for c, _, _ in backend.sandbox.commands]
        assert any("build-params" in c for c in commands), commands
        assert not any(c.startswith("bicep build ") for c in commands), (
            f"a parameter file must not go through `bicep build`: {commands}"
        )

    def test_a_template_still_uses_plain_build(self):
        store = InMemoryStore({"main.bicep": "param x string"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        commands = [c for c, _, _ in backend.sandbox.commands]
        assert any(c.startswith("bicep build ") for c in commands), commands
        assert not any("build-params" in c for c in commands), commands

    def test_both_kinds_still_get_linted(self):
        for filename, body in (("main.bicep", "param x string"), ("p.bicepparam", "using 'x'")):
            store = InMemoryStore({filename: body})
            backend = _fake_backend()
            _run(_tool(store, backend), [filename])
            commands = [c for c, _, _ in backend.sandbox.commands]
            assert any(c.startswith("bicep lint ") for c in commands), (filename, commands)

    def test_the_build_command_keeps_the_stderr_merge(self):
        """Both build variants must keep `2>&1`; SARIF goes to stderr for each."""
        from maf_sandbox_bicep._tool import _BUILD_CMD, _BUILD_PARAMS_CMD

        assert "2>&1" in _BUILD_CMD
        assert "2>&1" in _BUILD_PARAMS_CMD


class TestExecutionIsVisible:
    """A successful run must leave a record that the compiler actually executed.

    The tool's own output cannot answer it: "no diagnostics" is what a clean file looks
    like *and* what a call that never reached Bicep would look like if the SARIF happened
    to parse empty.  Without this line the only observable difference between "validated,
    all good" and "quietly did nothing" is latency.
    """

    def test_a_clean_run_logs_the_phase_file_and_diagnostic_count(self, caplog):
        store = InMemoryStore({"main.bicep": "param unused string"})
        backend = _fake_backend()

        with caplog.at_level(logging.INFO, logger="maf_sandbox_bicep"):
            _run(_tool(store, backend), ["main.bicep"])

        ok = [r.getMessage() for r in caplog.records if "bicep_validate: " in r.getMessage()]
        assert any(m.startswith("bicep_validate: build ok") for m in ok), ok
        assert any(m.startswith("bicep_validate: lint ok") for m in ok), ok
        assert all("file='main.bicep'" in m for m in ok), ok
        assert all("diagnostics=0" in m for m in ok), ok
        assert all("elapsed_ms=" in m for m in ok), ok

    def test_a_failed_exec_logs_no_success_line(self, caplog):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()

        async def _boom(command, *, working_directory, timeout):
            raise RuntimeError("exec blew up")

        backend.sandbox.exec = _boom  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="maf_sandbox_bicep"):
            out = _run(_tool(store, backend), ["main.bicep"])

        assert "Error: exec failed" in out
        assert not any("ok file=" in r.getMessage() for r in caplog.records), caplog.text


class TestFailureDetailIsLogged:
    """A provider failure must log enough to act on, while telling the model nothing.

    `str()` on an azure-core error is "Operation returned an invalid status 'Bad Request'".
    That sentence is unactionable — a live 400 meaning "this identity has no role on the
    group" and one meaning "the disk image is gone" are indistinguishable — so the log takes
    the status and the response body too. The model still gets the sanitized line, because
    tool results are persisted into the transcript and the body carries endpoint,
    subscription and tenant ids.
    """

    def test_the_log_carries_status_and_body_but_the_model_does_not(self, caplog):
        class _HttpError(Exception):
            status_code = 400

            def __str__(self) -> str:
                return "Operation returned an invalid status 'Bad Request'"

            class response:  # noqa: N801 - mimics the SDK's attribute shape
                @staticmethod
                def text() -> str:
                    return '{"error":"principal lacks a role on sandbox group acas-x"}'

        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()

        async def _boom(key, spec):
            raise _HttpError()

        backend.acquire = _boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_bicep"):
            out = _run(_tool(store, backend), ["main.bicep"])

        logged = caplog.text
        assert "status=400" in logged, logged
        assert "principal lacks a role" in logged, logged
        # The model is told it degraded, and nothing about the account.
        assert out == "Error: sandbox unavailable — degrading to T0 (LLM self-check only)"
        assert "principal lacks a role" not in out
        assert "acas-x" not in out


class TestEndToEnd:
    def test_writes_the_file_into_the_sandbox_and_reports_clean(self):
        store = InMemoryStore({"main.bicep": "param unused string"})
        backend = _fake_backend()
        out = _run(_tool(store, backend), ["main.bicep"])

        assert list(backend.sandbox.files.values()) == ["param unused string"]
        (path,) = backend.sandbox.files
        assert path.endswith("/main.bicep")
        assert "build(main.bicep): no diagnostics" in out
        assert "lint(main.bicep): no diagnostics" in out

    def test_runs_build_then_lint_with_the_fixed_templates(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        (path,) = backend.sandbox.files
        commands = [c for c, _, _ in backend.sandbox.commands]
        assert commands == [_BUILD_CMD.format(path=path), _LINT_CMD.format(path=path)]

    def test_renders_diagnostics_from_sarif(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend(
            InProcessSandbox(outputs={"bicep lint": _sarif()}, default_stdout=_EMPTY_SARIF)
        )
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "lint(main.bicep): 1 diagnostic(s)" in out
        assert "[error] no-unused-params @ main.bicep:5:7" in out

    def test_unparseable_output_is_an_error_not_a_clean_build(self):
        """A broken sandbox must never read as "no diagnostics"."""
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend(
            InProcessSandbox(
                outputs={"bicep build": "Segmentation fault"}, default_stdout=_EMPTY_SARIF
            )
        )
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "build(main.bicep): Error: could not parse SARIF output" in out

    def test_the_exec_timeout_is_passed_through(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend, exec_timeout_seconds=7), ["main.bicep"])

        assert {t for _, _, t in backend.sandbox.commands} == {7}

    def test_a_timeout_is_reported_per_phase_rather_than_hanging(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend(
            InProcessSandbox(raises=TimeoutError(), default_stdout=_EMPTY_SARIF)
        )
        out = _run(_tool(store, backend, exec_timeout_seconds=3), ["main.bicep"])

        assert "build(main.bicep): Error: timed out after 3s" in out
        assert "lint(main.bicep): Error: timed out after 3s" in out

    def test_validates_every_file_it_is_given(self):
        store = InMemoryStore({"a.bicep": "1", "b.bicep": "2"})
        backend = _fake_backend()
        out = _run(_tool(store, backend), ["a.bicep", "b.bicep"])

        # Paths are <work dir>/<per-call dir>/<workspace path>; assert the last part rather
        # than pinning a directory that changes every call (see TestStaleFilesAcrossRounds).
        assert {_workspace_part(p) for p in backend.sandbox.files} == {"a.bicep", "b.bicep"}
        assert out.count("no diagnostics") == 4  # build + lint, per file

    def test_the_key_carries_the_hosts_scope_and_thread_not_model_input(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        key = backend.keys[0]
        assert (key.scope, key.thread_id, key.agent_dir) == (
            "scope-a",
            "thread-1",
            "devops-engineer",
        )

    def test_the_spec_it_asks_for_allows_only_the_restore_hosts(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        assert backend.specs[0].egress_allow == (
            "mcr.microsoft.com",
            "*.data.mcr.microsoft.com",
            "aka.ms",
            "live-data.bicep.azure.com",
        )
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
        store = InMemoryStore({"main.bicep": "x", "modules/storage.bicep": "y"})
        backend = _fake_backend()
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
        store = InMemoryStore({"main.bicep": "x", "modules/storage.bicep": "y"})
        backend = _fake_backend()
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
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        from maf_sandbox_bicep._tool import _WORK_DIR

        (path,) = backend.sandbox.files
        assert path.startswith(f"{_WORK_DIR}/")
        # Not the root itself — that is where bicepconfig.json lives.
        assert path != f"{_WORK_DIR}/main.bicep"

    def test_the_compiler_runs_in_the_round_directory(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        (path,) = backend.sandbox.files
        round_dir = path.rsplit("/", 1)[0]
        assert {wd for _, wd, _ in backend.sandbox.commands} == {round_dir}


class TestDeployWorkflowStaysOffTheApplication:
    """The sandbox deploy must not need the Python workspace at all.

    It once ran `uv sync --extra bicep-sandbox` at the workspace root, which builds the host
    application, its TUI, its foundry skills, numpy and ruff — 128 packages and a git clone
    of agent-framework — to run one import script whose own closure is 34 and which imports
    nothing from the host.  Slow, and worse than slow: it made the sandbox deploy depend on
    the application's dependency tree, which is precisely the property this stack's
    separation is supposed to guarantee.

    The import now runs through the vendor's `aca` CLI, so the deploy touches no Python
    whatsoever — the separation is structural rather than a matter of passing the right
    flag.  This test holds it there.

    Nothing else would catch a regression.  The workflow would still succeed; it would just
    quietly be building the app again, and the symptom is a doc going stale rather than
    anything going red.
    """

    def _workflow(self):
        import pathlib

        import maf_sandbox_bicep

        distribution = pathlib.Path(maf_sandbox_bicep.__file__).parents[2]
        for root in (distribution.parents[1], distribution):
            path = root / ".github" / "workflows" / "deploy-bicep-sandbox.yml"
            if path.is_file():
                return path
        return None

    def test_the_deploy_needs_no_python_at_all(self):
        workflow = self._workflow()
        if workflow is None:
            # GitHub reads workflows only from the repository root, so an extracted copy of
            # this package re-creates them rather than carrying them along.
            pytest.skip("deploy-bicep-sandbox.yml is not present (extracted repository)")

        text = workflow.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
            and any(marker in line for marker in ("uv sync", "uv run", "setup-uv", "setup-python"))
        ]
        assert offenders == [], (
            f"the sandbox deploy reaches for the Python workspace: {offenders}. The import "
            "runs through the `aca` CLI precisely so this deploy needs no toolchain and "
            "nothing from this repository — a `uv sync` here installs the host application "
            "(128 packages) and couples the stack to the app's dependency tree."
        )

    def test_the_registry_token_is_masked_before_it_is_used(self):
        """The minted ACR access token must be masked in the same step that mints it.

        A disk-image build from a private registry takes an explicit username/token pair
        rather than a managed identity, so a real — if short-lived — pull credential exists
        in the runner's environment, and `set -x`, a `--debug` flag or an `echo` added later
        would put it in a public log.  `::add-mask::` costs one line and is the difference
        between "it leaked" and "it didn't"; ordering matters, because a mask applied after
        the fact does not retroactively scrub what was already printed.
        """
        workflow = self._workflow()
        if workflow is None:
            pytest.skip("deploy-bicep-sandbox.yml is not present (extracted repository)")

        text = workflow.read_text(encoding="utf-8")
        if "--expose-token" not in text:
            pytest.skip("the deploy no longer mints a registry token")

        minted = text.index("--expose-token")
        masked = text.find("::add-mask::$ACR_TOKEN")
        assert masked != -1, (
            "the deploy mints an ACR access token but never masks it — add "
            'echo "::add-mask::$ACR_TOKEN" immediately after minting it'
        )
        assert masked > minted, (
            "the token is masked before it is minted, which masks nothing; the "
            "::add-mask:: must follow the `az acr login --expose-token` that creates it"
        )


class TestConfigDiscovery:
    """The image must ship `bicepconfig.json` at the root the tool writes under.

    Bicep resolves that file only by walking up from the source, and the pinned CLI has no
    `--config-file` flag on `build` or `lint`. So if the image's path and `_WORK_DIR` ever
    drift apart, the config is simply never found and `bicep lint` falls back to its
    built-in defaults — while still returning parseable SARIF and rendering diagnostics
    normally. Nothing else in this suite would notice, which is the whole reason this test
    reaches outside the package to read the Dockerfile.

    The image is a *deployment* artifact and lives with whichever repository builds it — it
    did not come along when these packages were extracted.  So this runs where the image is
    present and skips where it is not; the deploying repository owns the other half of the
    guard, asserting its Dockerfile against this package's published ``_WORK_DIR``.  Both
    halves read the same constant, which is what keeps them from drifting apart.
    """

    def _dockerfile(self):
        import pathlib

        import maf_sandbox_bicep

        distribution = pathlib.Path(maf_sandbox_bicep.__file__).parents[2]
        candidates = [
            # Beside the package, when a repository holds both.
            distribution.parents[1] / "images" / "bicep-sandbox" / "Dockerfile",
            distribution / "images" / "bicep-sandbox" / "Dockerfile",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def test_the_image_puts_bicepconfig_at_the_work_dir_root(self):
        from maf_sandbox_bicep._tool import _WORK_DIR

        dockerfile = self._dockerfile()
        if dockerfile is None:
            pytest.skip(
                "the bicep-sandbox image is not in this repository — the repository that "
                "builds it asserts its Dockerfile against maf_sandbox_bicep's _WORK_DIR"
            )

        text = dockerfile.read_text(encoding="utf-8")
        assert f"COPY bicepconfig.json {_WORK_DIR}/bicepconfig.json" in text, (
            f"the image must COPY bicepconfig.json to {_WORK_DIR}/, the root the tool writes "
            "each validation under — Bicep finds it only by walking up from the source file"
        )

    def test_the_round_directory_is_a_child_of_that_root(self):
        """One level down, so the walk-up reaches the config in a single step."""
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        from maf_sandbox_bicep._tool import _WORK_DIR

        (path,) = backend.sandbox.files
        assert path.startswith(f"{_WORK_DIR}/")
        assert path.count("/") == _WORK_DIR.count("/") + 2  # <root>/<round>/main.bicep

    def test_the_compiler_runs_in_the_round_directory(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        _run(_tool(store, backend), ["main.bicep"])

        (path,) = backend.sandbox.files
        round_dir = path.rsplit("/", 1)[0]
        assert {wd for _, wd, _ in backend.sandbox.commands} == {round_dir}


class TestEndToEndRefusals:
    def test_rejects_a_non_bicep_extension_before_touching_the_sandbox(self):
        store = InMemoryStore({"main.tf": "x"})
        backend = _fake_backend()
        out = _run(_tool(store, backend), ["main.tf"])

        assert "only accepts .bicep and .bicepparam" in out
        assert backend.keys == []

    def test_rejects_a_file_that_is_not_in_the_workspace_listing(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
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
        store = InMemoryStore({malicious: "x"})
        backend = _fake_backend()
        out = _run(_tool(store, backend), [malicious])

        assert "unsafe characters" in out
        assert backend.sandbox.commands == []

    def test_the_extension_gate_runs_before_the_path_guard(self):
        """Pins the ordering the test above depends on."""
        store = InMemoryStore({"main.bicep; rm -rf /": "x"})
        backend = _fake_backend()
        out = _run(_tool(store, backend), ["main.bicep; rm -rf /"])

        assert "only accepts .bicep and .bicepparam" in out
        assert backend.sandbox.commands == []

    def test_no_thread_context_is_refused(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend()
        out = _run(_tool(store, backend, thread_id=None), ["main.bicep"])

        assert "no active thread context" in out

    def test_an_unavailable_sandbox_degrades_to_t0_without_leaking_sdk_detail(self):
        """SDK errors carry endpoint/subscription/tenant and tool results are persisted."""
        store = InMemoryStore({"main.bicep": "x"})
        secret = "https://management.eastus.azuredevcompute.io subscription 0000-1111"
        backend = _fake_backend(acquire_error=RuntimeError(secret))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "degrading to T0" in out
        assert "azuredevcompute" not in out
        assert "0000-1111" not in out

    def test_a_configuration_error_is_surfaced_because_we_authored_it(self):
        store = InMemoryStore({"main.bicep": "x"})
        backend = _fake_backend(acquire_error=ValueError("No disk image ... was built from 'x'"))
        out = _run(_tool(store, backend), ["main.bicep"])

        assert "No disk image" in out


# ---------------------------------------------------------------------------
# Attach / do not attach
# ---------------------------------------------------------------------------


class TestMakeBicepTools:
    """A host with no sandbox gets no tool, not a tool that fails when called."""

    def test_returns_empty_without_a_router(self):
        assert (
            make_bicep_tools(
                None, InMemoryStore({}), "devops-engineer", _context(InMemoryStore({}))
            )
            == []
        )

    def test_returns_empty_when_the_router_has_no_backend(self):
        store = InMemoryStore({})
        router = SandboxRouter([])
        assert make_bicep_tools(router, store, "devops-engineer", _context(store)) == []

    def test_tool_has_correct_name(self):
        store = InMemoryStore({})
        tool = _tool(store, _fake_backend())
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__tool_definition__", None), "name", None
        )
        assert name == BICEP_VALIDATE_TOOL_NAME

    def test_tool_names_table_matches_the_tool(self):
        assert BICEP_TOOL_NAMES == frozenset({BICEP_VALIDATE_TOOL_NAME})


class TestFidesDeclarations:
    """`additional_properties` is read by a live policy engine, not filed as documentation.

    MAF's information-flow module (`agent_framework.security`) reads these exact keys before
    every call, and a host layers its own classification on top. So the set of keys is a
    behavioural contract: adding a `confidentiality` or `max_allowed_confidentiality` key
    here can start gating calls in a deployment whose confidentiality leg currently cannot
    fire, and nothing in this suite — or in the host's — would report the change as a
    failure. It would simply become a different policy.

    The factory in :mod:`maf_sandbox.maf` CAN derive an egress cap; this kind deliberately
    does not ask for one (see the comment at its `sandboxed_tool` call). This test is what
    holds that decision in place.
    """

    def _properties(self):
        store = InMemoryStore({})
        return dict(_tool(store, _fake_backend()).additional_properties or {})

    def test_the_tool_declares_exactly_trusted_source_integrity(self):
        assert self._properties() == {"source_integrity": "trusted"}

    def test_it_declares_nothing_about_confidentiality(self):
        properties = self._properties()
        assert "confidentiality" not in properties
        assert "max_allowed_confidentiality" not in properties


# ---------------------------------------------------------------------------
# The spec — containment that must not be configurable away
# ---------------------------------------------------------------------------


class TestBicepSandboxSpec:
    def test_allows_exactly_the_four_restore_hosts(self):
        """Two pairs, and every one of them is load-bearing for a restore.

        Manifests come from mcr.microsoft.com, layer blobs from *.data.mcr.microsoft.com:
        with only the first, restore resolves the manifest and then 403s on the blob —
        BCP192 on every `br/public:` reference, so module types never load and module-input
        type errors are invisible to the whole validation.

        The public module index is requested from aka.ms and served from
        live-data.bicep.azure.com: with only the redirector, the fetch ends at a `Location`
        header pointing somewhere denied. Blocked entirely, `use-recent-module-versions`
        reports a download error once per file instead of the outdated module pins it exists
        to find.

        Anything beyond these four Microsoft-operated hosts widens containment and must not
        appear.
        """
        assert bicep_sandbox_spec().egress_allow == (
            "mcr.microsoft.com",
            "*.data.mcr.microsoft.com",
            "aka.ms",
            "live-data.bicep.azure.com",
        )

    def test_work_dir_is_a_dedicated_root(self):
        """Everything shared with the sandbox lives here, on a path nothing else owns."""
        assert bicep_sandbox_spec().work_dir == "/acas/work"

    def test_kind_is_bicep(self):
        assert bicep_sandbox_spec().kind == "bicep"


# ---------------------------------------------------------------------------
# Restore failure — a broken validation must not read as a diagnostic list
# ---------------------------------------------------------------------------


class TestRestoreFailureBanner:
    """BCP190/191/192 mean module types never loaded, so module-input checks did not run.

    Rendered as an ordinary diagnostic list, a restore-failed run invites exactly the
    misreading that shipped broken Bicep in production: an agent discounts the restore noise
    as environment failure, certifies the module inputs from READMEs, and reports PASS on
    files that do not compile.  The banner names the run incomplete so it cannot be read as
    evidence of health.
    """

    @pytest.mark.parametrize("rule", ["BCP190", "BCP191", "BCP192"])
    def test_a_restore_failure_gets_the_incomplete_validation_banner(self, rule):
        store = InMemoryStore({"main.bicep": "x"})
        sandbox = InProcessSandbox(
            outputs={"bicep build": _sarif(rule=rule, message="Unable to restore …: 403")},
            default_stdout=_EMPTY_SARIF,
        )
        out = _run(_tool(store, _fake_backend(sandbox=sandbox)), ["main.bicep"])

        assert "MODULE RESTORE FAILED" in out
        assert "INCOMPLETE" in out
        # The underlying diagnostics still follow the banner — evidence, not replacement.
        assert rule in out

    def test_a_clean_run_carries_no_banner(self):
        store = InMemoryStore({"main.bicep": "x"})
        out = _run(_tool(store, _fake_backend()), ["main.bicep"])

        assert "MODULE RESTORE FAILED" not in out

    def test_ordinary_errors_do_not_trigger_it(self):
        """Real defects must arrive undecorated — the banner is about absent evidence."""
        store = InMemoryStore({"main.bicep": "x"})
        sandbox = InProcessSandbox(
            outputs={"bicep build": _sarif(rule="BCP035", message="Missing 'properties'.")},
            default_stdout=_EMPTY_SARIF,
        )
        out = _run(_tool(store, _fake_backend(sandbox=sandbox)), ["main.bicep"])

        assert "BCP035" in out
        assert "MODULE RESTORE FAILED" not in out


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
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `pip install
#: agent-framework-core` puts `agent_framework` on the path, `maf-sandbox` puts
#: `maf_sandbox` on it, and `azure-identity` and `azure-containerapps-sandbox` both extend
#: the single `azure` namespace package rather than each owning a top-level name of their
#: own. Anything not listed here is assumed to import under its distribution name with
#: hyphens turned to underscores — true of every dependency any of the three maf-sandbox*
#: packages declares today. A dependency where that guess is wrong fails the test below
#: with a readable "imports X" message, which is the right place to notice a new exception
#: belongs here.
_DISTRIBUTION_TO_IMPORT_NAME = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
    "azure-identity": "azure",
    "azure-containerapps-sandbox": "azure",
}


def _package_modules():
    """Every module in the installed `maf_sandbox_bicep`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox_bicep

    root = pathlib.Path(maf_sandbox_bicep.__file__).parent  # type: ignore[arg-type]
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
    """The import names `pyproject.toml` licenses `maf_sandbox_bicep` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox_bicep

    root = pathlib.Path(maf_sandbox_bicep.__file__).parents[2]  # type: ignore[arg-type]
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

    This is the invariant that replaced ``TestNoHostDependency`` (a source scan for the name
    of the private application these packages were extracted from, back when this package
    lived inside it). That name was one instance of a broader risk: a module reaching for
    anything not on *this package's own* dependency list. Nothing else here would notice —
    the workspace running this suite has every sibling package, and everything a host
    application needs, already importable, so a stray import resolves fine in this
    environment regardless of what it names. The first sign of trouble is a downstream
    consumer who installs the published wheel alone, and what they get is an
    ``ImportError`` with no test pointing at the cause.

    Reading ``pyproject.toml`` at test time, rather than hard-coding the allowed names, is
    what keeps this from becoming a second list to update by hand alongside the first: the
    two would drift, and a stale allowlist is a test that passes for the wrong reason.
    """

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 4

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox_bicep package — "
                "this check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(sys.stdlib_module_names) | declared | {"maf_sandbox_bicep"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox_bicep modules import something outside the standard "
            f"library, the package itself, and pyproject.toml's declared dependencies: "
            f"{offenders}. Either the import is a mistake, or the dependency belongs in "
            "pyproject.toml."
        )


class TestNoDirectAzureImport:
    """Acceptance criterion for this split: the same tool must run on any backend.

    ``azure`` is not a declared dependency of this package, so
    ``TestOnlyDeclaredDependencies`` above already catches an ``import azure`` here — this
    test is kept alongside it because it names the specific portability property (rather
    than a generic "undeclared dependency") and its failure message says what actually
    broke: the workload reaching around ``maf_sandbox`` for a provider directly.
    """

    def test_the_workload_does_not_import_azure(self):
        import pathlib
        import re

        import maf_sandbox_bicep

        root = pathlib.Path(maf_sandbox_bicep.__file__).parent  # type: ignore[arg-type]
        pattern = re.compile(r"(?m)^\s*(?:from\s+azure[.\s]|import\s+azure[.\s])")
        offenders = [
            str(p) for p in root.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"the bicep workload imports Azure directly: {offenders}. "
            "It must reach a sandbox through maf_sandbox, or it stops being portable."
        )
