"""The adapter against the in-process fake: what Deep Agents is handed, and what the router keeps.

No container and no model. The fake declares `FILES_OUT` here because the adapter requires it,
and the router runs at `Isolation.NONE` because the fake declares no more — both are the test
wiring, not a posture a host would choose.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol, execute_accepts_timeout
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_deepagents import (
    DEEPAGENTS_KIND,
    DEFAULT_EXEC_TIMEOUT_SECONDS,
    REQUIRED_CAPABILITIES,
    SANDBOX_UNAVAILABLE,
    MafSandbox,
    deepagents_spec,
)
from maf_sandbox_deepagents._sandbox import _response

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="coder")
WORK = "/maf-sandbox/work"

#: The fake plus the pull surface the adapter needs for `download_files`.
DECLARATIONS = dataclasses.replace(
    FAKE_BACKEND_DECLARATIONS,
    capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.FILES_OUT},
)


def _backend(sandbox: InProcessSandbox | None = None, **kwargs) -> InProcessSandboxBackend:
    return InProcessSandboxBackend(sandbox, declarations=DECLARATIONS, **kwargs)


def _router(backend: InProcessSandboxBackend) -> SandboxRouter:
    return SandboxRouter([backend], min_isolation=Isolation.NONE)


def _adapter(
    sandbox: InProcessSandbox | None = None, **kwargs
) -> tuple[MafSandbox, InProcessSandboxBackend]:
    backend = _backend(sandbox, **kwargs)
    return MafSandbox(_router(backend), KEY, deepagents_spec("img:1")), backend


class TestTheSpec:
    def test_requires_what_deep_agents_needs(self):
        spec = deepagents_spec("img:1")
        assert spec.kind == DEEPAGENTS_KIND
        assert spec.requires == REQUIRED_CAPABILITIES
        assert Capability.FILES_OUT in spec.requires

    def test_egress_is_closed_unless_hosts_are_named(self):
        assert deepagents_spec("img:1").egress is Egress.CLOSED
        allowed = deepagents_spec("img:1", egress_allow=("pypi.org",))
        assert allowed.egress is Egress.ALLOWLIST
        assert allowed.egress_allow == ("pypi.org",)

    def test_the_work_dir_default_is_the_protocol_s(self):
        assert deepagents_spec("img:1").work_dir == SandboxSpec(kind="x").work_dir
        assert deepagents_spec("img:1", work_dir="/w").work_dir == "/w"
        assert deepagents_spec("img:1", work_dir=None).work_dir is None


class TestConstruction:
    def test_is_a_deep_agents_sandbox_with_a_per_command_timeout(self):
        adapter, _ = _adapter()
        assert isinstance(adapter, SandboxBackendProtocol)
        assert execute_accepts_timeout(MafSandbox)

    def test_refuses_a_spec_missing_a_required_capability(self):
        spec = dataclasses.replace(deepagents_spec("img:1"), requires=frozenset({Capability.EXEC}))
        with pytest.raises(ValueError, match="files_in"):
            MafSandbox(_router(_backend()), KEY, spec)

    def test_refuses_a_per_call_scope(self):
        spec = dataclasses.replace(deepagents_spec("img:1"), isolation_scope=IsolationScope.CALL)
        with pytest.raises(ValueError, match="whole conversation"):
            MafSandbox(_router(_backend()), KEY, spec)

    def test_refuses_a_key_naming_a_call(self):
        with pytest.raises(ValueError, match="call_id"):
            MafSandbox(
                _router(_backend()),
                dataclasses.replace(KEY, call_id="c1"),
                deepagents_spec("img:1"),
            )

    def test_refuses_a_backend_without_the_pull_surface(self):
        """The router's capability match, asked at construction rather than at the first command."""
        backend = InProcessSandboxBackend(declarations=FAKE_BACKEND_DECLARATIONS)
        with pytest.raises(SandboxCapabilityNotSupported):
            MafSandbox(_router(backend), KEY, deepagents_spec("img:1"))

    def test_refuses_a_spec_raising_the_floor_above_the_backend(self):
        with pytest.raises(SandboxBackendNotPermitted):
            MafSandbox(
                _router(_backend()), KEY, deepagents_spec("img:1", min_isolation=Isolation.MICROVM)
            )

    def test_refuses_a_non_positive_timeout(self):
        with pytest.raises(ValueError, match="exec_timeout_seconds"):
            MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1"), exec_timeout_seconds=0)


class TestTheId:
    def test_is_opaque_and_stable(self):
        first, _ = _adapter()
        second, _ = _adapter()
        assert first.id == second.id
        assert first.id.startswith("maf-sandbox-")
        for part in (KEY.scope, KEY.thread_id, KEY.agent_dir):
            assert part not in first.id

    def test_differs_by_conversation_and_by_kind(self):
        base, _ = _adapter()
        other_thread = MafSandbox(
            _router(_backend()),
            dataclasses.replace(KEY, thread_id="thread-2"),
            deepagents_spec("img:1"),
        )
        other_kind = MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", kind="shell"))
        assert len({base.id, other_thread.id, other_kind.id}) == 3


class TestExecute:
    def test_runs_the_command_in_the_storage_base_under_the_default_timeout(self):
        """The base is addressed as `"."`; the backend resolves it to the spec's `work_dir`."""
        fake = InProcessSandbox(outputs={"echo": "hello\n"})
        adapter, _ = _adapter(fake)

        response = asyncio.run(adapter.aexecute("echo hello"))

        assert response.output == "hello\n"
        assert response.exit_code == 0
        assert response.truncated is False
        assert fake.commands == [("echo hello", WORK, DEFAULT_EXEC_TIMEOUT_SECONDS)]
        assert adapter.spec.work_dir == WORK

    def test_a_per_command_timeout_is_the_bound_handed_down(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        asyncio.run(adapter.aexecute("true", timeout=7))
        assert fake.commands[0][2] == 7.0

    def test_an_empty_command_is_an_error_result_not_a_raise(self):
        adapter, _ = _adapter()
        response = asyncio.run(adapter.aexecute(""))
        assert response.exit_code == 1
        assert "non-empty" in response.output

    def test_a_non_positive_timeout_raises(self):
        adapter, _ = _adapter()
        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(adapter.aexecute("true", timeout=0))

    def test_the_sandbox_is_reused_warm_under_this_key_and_kind(self):
        """The router cleans an instance it has never seen before the first command, which is
        one extra create and dispose on a fresh conversation; every command after that reuses."""
        adapter, backend = _adapter(InProcessSandbox())
        asyncio.run(adapter.aexecute("one"))
        adopted = len(backend.keys)
        asyncio.run(adapter.aexecute("two"))
        assert len(backend.keys) == adopted + 1
        assert set(backend.keys) == {KEY}
        assert {spec.kind for spec in backend.specs} == {DEEPAGENTS_KIND}

    def test_an_unavailable_sandbox_is_a_fixed_sentence_with_the_detail_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        detail = "subscription 0000-1111 refused the create"
        adapter, _ = _adapter(acquire_error=RuntimeError(detail))
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))
        assert response.output == SANDBOX_UNAVAILABLE
        assert response.exit_code is None
        assert "subscription" not in response.output
        assert detail in caplog.text

    def test_a_timeout_is_reported_as_one(self):
        adapter, _ = _adapter(InProcessSandbox(raises=TimeoutError()))
        response = asyncio.run(adapter.aexecute("sleep 999", timeout=3))
        assert response.exit_code is None
        assert "3 seconds" in response.output


class TestTheCombinedStream:
    def test_stderr_lines_are_labelled(self):
        response = _response(ExecResult(stdout="out\n", stderr="warn 1\nwarn 2\n", exit_code=2))
        assert response.output == "out\n\n[stderr] warn 1\n[stderr] warn 2"
        assert response.exit_code == 2

    def test_a_producer_s_note_is_labelled_as_the_host_s(self):
        response = _response(
            ExecResult(stdout="", stderr="output dropped", producer_owns_stderr=True)
        )
        assert response.output == "[note] output dropped"

    def test_nothing_is_said_so(self):
        assert _response(ExecResult(stdout="")).output == "<no output>"


class TestFilesIn:
    def test_uploads_land_under_the_work_dir(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.aupload_files([("main.bicep", b"param x string"), ("sub/two.txt", b"2")])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("main.bicep", None),
            ("sub/two.txt", None),
        ]
        assert fake.contents[f"{WORK}/main.bicep"] == b"param x string"
        assert fake.contents[f"{WORK}/sub/two.txt"] == b"2"

    def test_a_path_leaving_the_work_dir_is_refused_by_code(self):
        adapter, _ = _adapter(InProcessSandbox())
        (response,) = asyncio.run(adapter.aupload_files([("../etc/passwd", b"x")]))
        assert response.error == "invalid_path"

    def test_an_unavailable_sandbox_fails_every_file_without_raising(self):
        adapter, _ = _adapter(acquire_error=RuntimeError("down"))
        responses = asyncio.run(adapter.aupload_files([("a", b""), ("b", b"")]))
        assert [r.error for r in responses] == ["upload failed; see the host log"] * 2


class TestFilesOut:
    def test_a_round_trip_comes_back_byte_identical(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        payload = bytes(range(256))
        asyncio.run(adapter.aupload_files([("blob.bin", payload)]))

        (response,) = asyncio.run(adapter.adownload_files(["blob.bin"]))

        assert response.error is None
        assert response.content == payload

    def test_each_refusal_has_its_code(self):
        fake = InProcessSandbox(
            seed_files={
                f"{WORK}/dir": EntryKind.DIRECTORY,
                f"{WORK}/link": EntryKind.SYMLINK,
                f"{WORK}/there.txt": "content",
            }
        )
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.adownload_files(["missing.txt", "dir", "link", "../outside", "there.txt"])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("missing.txt", "file_not_found"),
            ("dir", "is_directory"),
            ("link", "invalid_path"),
            ("../outside", "invalid_path"),
            ("there.txt", None),
        ]
        assert responses[-1].content == b"content"

    def test_a_file_over_the_cap_is_refused_rather_than_truncated(self):
        fake = InProcessSandbox(seed_files={f"{WORK}/big.bin": "0123456789"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=4, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        (response,) = asyncio.run(adapter.adownload_files(["big.bin"]))

        assert response.content is None
        assert response.error is not None
        assert "max_bytes_per_file" in response.error


class TestTheSynchronousSurface:
    def test_works_with_no_loop_running(self):
        fake = InProcessSandbox(outputs={"echo": "hi"})
        adapter, _ = _adapter(fake)
        assert adapter.execute("echo hi").output == "hi"
        assert adapter.upload_files([("f", b"1")])[0].error is None
        assert adapter.download_files(["f"])[0].content == b"1"

    def test_works_from_inside_a_running_loop(self):
        """A sync tool called on the loop's own thread must not trip `asyncio.run`'s nesting refusal."""
        fake = InProcessSandbox(outputs={"echo": "hi"})
        adapter, _ = _adapter(fake)

        async def scenario() -> str:
            return adapter.execute("echo hi").output

        assert asyncio.run(scenario()) == "hi"

    def test_an_exception_crosses_back_to_the_caller(self):
        adapter, _ = _adapter()

        async def scenario() -> None:
            adapter.execute("true", timeout=0)

        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(scenario())


class TestClose:
    def test_disposes_this_kind_for_this_conversation(self):
        adapter, backend = _adapter(InProcessSandbox())
        asyncio.run(adapter.aexecute("true"))

        before = len(backend.disposed)

        assert asyncio.run(adapter.aclose()) is True

        assert backend.disposed[before:] == [KEY]
        assert backend.disposed_kinds[before:] == [DEEPAGENTS_KIND]

    def test_the_synchronous_close_is_the_same_call(self):
        adapter, backend = _adapter(InProcessSandbox())
        adapter.execute("true")
        before = len(backend.disposed)
        assert adapter.close() is True
        assert backend.disposed[before:] == [KEY]

    def test_the_next_command_after_a_close_starts_a_fresh_sandbox(self):
        fake = InProcessSandbox()
        adapter, backend = _adapter(fake, sandbox_per_key=True)
        asyncio.run(adapter.aupload_files([("keep.txt", b"1")]))
        asyncio.run(adapter.aclose())
        before = len(backend.keys)

        (response,) = asyncio.run(adapter.adownload_files(["keep.txt"]))

        assert response.error == "file_not_found"
        assert len(backend.keys) > before
