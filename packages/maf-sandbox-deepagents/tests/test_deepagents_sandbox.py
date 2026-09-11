"""The adapter against the in-process fake: what Deep Agents is handed, and what the router keeps.

No container and no model. The fake declares `FILES_OUT` here because the adapter requires it,
and the router runs at `Isolation.NONE` because the fake declares no more — both are the test
wiring, not a posture a host would choose.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol, execute_accepts_timeout
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    NoSandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
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
        # Left unset, as the shipped kinds leave it: the plain Docker constructor declares no
        # guest family, and the tools' needs (`sh`, `python3`) are the image's, not the shape's.
        assert spec.requires_os_family is None

    def test_egress_is_closed_unless_hosts_are_named(self):
        assert deepagents_spec("img:1").egress is Egress.CLOSED
        allowed = deepagents_spec("img:1", egress_allow=("pypi.org",))
        assert allowed.egress is Egress.ALLOWLIST
        assert allowed.egress_allow == ("pypi.org",)

    def test_the_work_dir_default_is_the_protocol_s(self):
        assert deepagents_spec("img:1").work_dir == SandboxSpec(kind="x").work_dir
        assert deepagents_spec("img:1", work_dir="/w").work_dir == "/w"


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

    @pytest.mark.parametrize("budget", [0, -1, 1.5, True])
    def test_refuses_an_output_budget_that_bounds_nothing(self, budget: object):
        with pytest.raises(ValueError, match="max_output_bytes"):
            MafSandbox(
                _router(_backend()),
                KEY,
                deepagents_spec("img:1"),
                max_output_bytes=budget,  # pyright: ignore[reportArgumentType]
            )

    def test_refuses_a_spec_raising_the_floor_above_the_backend(self):
        with pytest.raises(SandboxBackendNotPermitted):
            MafSandbox(
                _router(_backend()), KEY, deepagents_spec("img:1", min_isolation=Isolation.MICROVM)
            )

    @pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
    def test_refuses_a_timeout_that_bounds_nothing(self, seconds: float):
        with pytest.raises(ValueError, match="exec_timeout_seconds"):
            MafSandbox(
                _router(_backend()), KEY, deepagents_spec("img:1"), exec_timeout_seconds=seconds
            )

    def test_refuses_a_router_with_no_backend(self):
        with pytest.raises(NoSandboxBackend):
            MafSandbox(
                SandboxRouter([], min_isolation=Isolation.NONE), KEY, deepagents_spec("img:1")
            )

    def test_refuses_a_base_the_backend_would_allocate(self):
        with pytest.raises(ValueError, match="work_dir"):
            MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", work_dir=None))


class TestTheId:
    def test_is_opaque(self):
        first, _ = _adapter()
        assert first.id.startswith("maf-sandbox-")
        for part in (KEY.scope, KEY.thread_id, KEY.agent_dir):
            assert part not in first.id

    def test_names_the_sandbox_the_router_reaches(self):
        """Two adapters over one key, kind and backend reach one sandbox, and say so."""
        first, _ = _adapter()
        second, _ = _adapter()
        assert first.id == second.id

    def test_differs_by_conversation_kind_and_backend(self):
        base, _ = _adapter()
        other_thread = MafSandbox(
            _router(_backend()),
            dataclasses.replace(KEY, thread_id="thread-2"),
            deepagents_spec("img:1"),
        )
        other_kind = MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", kind="shell"))
        other_backend = MafSandbox(_router(_backend(name="second")), KEY, deepagents_spec("img:1"))
        other_egress = MafSandbox(
            _router(_backend()), KEY, deepagents_spec("img:1", egress_allow=("pypi.org",))
        )
        assert (
            len({base.id, other_thread.id, other_kind.id, other_backend.id, other_egress.id}) == 5
        )

    def test_the_encoding_keeps_field_boundaries(self):
        """A scope ending where a thread begins must not collide with the split moved."""
        shifted = SandboxKey(scope="tenant-", thread_id="athread-1", agent_dir="coder")
        base, _ = _adapter()
        other = MafSandbox(_router(_backend()), shifted, deepagents_spec("img:1"))
        assert base.id != other.id


class TestExecute:
    def test_runs_the_command_in_the_storage_base_under_the_default_timeout(self):
        """The base is addressed as `"."`; the backend resolves it to the spec's `work_dir`."""
        fake = InProcessSandbox(outputs={"echo": "hello\n"})
        adapter, _ = _adapter(fake)

        response = asyncio.run(adapter.aexecute("echo hello"))

        assert response.output == "hello\n"
        assert response.exit_code == 0
        assert response.truncated is False
        ((command, directory, bound),) = fake.commands
        assert (command, directory) == ("echo hello", WORK)
        assert DEFAULT_EXEC_TIMEOUT_SECONDS - 0.5 < bound <= DEFAULT_EXEC_TIMEOUT_SECONDS
        assert adapter.spec.work_dir == WORK

    def test_a_per_command_timeout_bounds_the_command_less_what_the_acquire_spent(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        asyncio.run(adapter.aexecute("true", timeout=7))
        assert 6.5 < fake.commands[0][2] <= 7.0

    def test_an_acquire_that_outlives_the_budget_is_cut_off(self, monkeypatch: pytest.MonkeyPatch):
        """The deadline bounds the acquire too: a queued or slow create cannot exceed it."""
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        adapter = MafSandbox(adapter.router, KEY, adapter.spec, exec_timeout_seconds=0.05)
        acquire = adapter.router.acquire

        async def slow_acquire(*args, **kwargs):
            await asyncio.sleep(5)
            return await acquire(*args, **kwargs)

        monkeypatch.setattr(adapter.router, "acquire", slow_acquire)

        started = time.monotonic()
        response = asyncio.run(adapter.aexecute("true"))

        assert time.monotonic() - started < 1
        assert response.exit_code is None
        assert "0.05 seconds" in response.output
        assert fake.commands == []

    def test_output_past_the_budget_is_dropped_whole_and_said_so(self):
        fake = InProcessSandbox(outputs={"seq": "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"})
        adapter, _ = _adapter(fake)
        adapter = MafSandbox(adapter.router, KEY, adapter.spec, max_output_bytes=8)

        response = asyncio.run(adapter.aexecute("seq 10"))

        assert response.truncated is True
        assert response.exit_code is None
        assert "8 bytes" in response.output
        assert "1" not in response.output.replace("8 bytes", "")

    def test_a_sandbox_that_cannot_bound_output_runs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ):
        class Unbounded(InProcessSandbox):
            exec_bounded = None  # type: ignore[assignment]  # opts out of `BoundedExec`

        fake = Unbounded()
        adapter, _ = _adapter(fake)
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))

        assert response.exit_code is None
        assert "did not run" in response.output
        assert fake.commands == []
        assert "exec_bounded" in caplog.text

    def test_an_empty_command_is_an_error_result_not_a_raise(self):
        adapter, _ = _adapter()
        response = asyncio.run(adapter.aexecute(""))
        assert response.exit_code == 1
        assert "non-empty" in response.output

    @pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
    def test_a_timeout_that_bounds_nothing_raises(self, seconds: float):
        adapter, _ = _adapter()
        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(adapter.aexecute("true", timeout=seconds))  # pyright: ignore[reportArgumentType]

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

    def test_a_failure_to_run_is_a_fixed_sentence_with_the_detail_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        detail = "docker exec: subscription 0000-1111 refused"
        adapter, _ = _adapter(InProcessSandbox(raises=RuntimeError(detail)))
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))
        assert response.exit_code is None
        assert "subscription" not in response.output
        assert response.output != SANDBOX_UNAVAILABLE
        assert "result" in response.output
        assert detail in caplog.text

    def test_a_timeout_is_reported_as_one_and_claims_no_stop(self):
        adapter, _ = _adapter(InProcessSandbox(raises=TimeoutError()))
        response = asyncio.run(adapter.aexecute("sleep 999", timeout=3))
        assert response.exit_code is None
        assert "3 seconds" in response.output
        assert "stopped" not in response.output


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

    def test_paths_are_guest_paths_under_the_base(self):
        """Deep Agents' file tools spell paths absolutely; the base is where they must land."""
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.aupload_files(
                [
                    (f"{WORK}/notes/todo.txt", b"1"),
                    ("../etc/passwd", b"x"),
                    ("/notes/todo.txt", b"2"),
                ]
            )
        )

        assert [(r.path, r.error) for r in responses] == [
            (f"{WORK}/notes/todo.txt", None),
            ("../etc/passwd", "invalid_path"),
            ("/notes/todo.txt", "invalid_path"),
        ]
        assert sorted(fake.contents) == [f"{WORK}/notes/todo.txt"]
        (read,) = asyncio.run(adapter.adownload_files([f"{WORK}/notes/todo.txt"]))
        assert read.content == b"1"

    def test_a_batch_over_max_files_is_refused_whole_before_anything_crosses(self):
        fake = InProcessSandbox()
        spec = deepagents_spec(
            "img:1", files_in=TransferLimits(max_bytes_per_file=8, max_total_bytes=8, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.aupload_files([("a", b"1"), ("b", b"2")]))

        assert [r.error for r in responses] == [
            "the batch has more files than files_in.max_files allows"
        ] * 2
        assert fake.contents == {}

    def test_each_file_is_held_to_the_per_file_and_total_caps(self):
        fake = InProcessSandbox()
        spec = deepagents_spec(
            "img:1", files_in=TransferLimits(max_bytes_per_file=4, max_total_bytes=6, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(
            adapter.aupload_files([("big", b"12345"), ("a", b"1234"), ("b", b"123"), ("c", b"12")])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("big", "the file is larger than files_in.max_bytes_per_file"),
            ("a", None),
            ("b", "the batch would exceed files_in.max_total_bytes"),
            ("c", None),
        ]
        assert sorted(fake.contents) == [f"{WORK}/a", f"{WORK}/c"]

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
        assert response.error == "the file is larger than files_out.max_bytes_per_file"

    def test_a_read_that_times_out_is_a_failure_not_a_bad_path(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        fake = InProcessSandbox(seed_files={f"{WORK}/slow.txt": "content"})
        adapter, _ = _adapter(fake)

        async def read_file(*args, **kwargs):
            raise TimeoutError("the read did not finish")

        monkeypatch.setattr(fake, "read_file", read_file)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_deepagents"):
            (response,) = asyncio.run(adapter.adownload_files(["slow.txt"]))

        assert response.error == "download failed; see the host log"
        assert "timed out" in caplog.text

    def test_a_cap_the_read_reports_is_named_by_the_cap_handed_down(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A file that grew after the stat is judged by the ceiling the read was given."""
        fake = InProcessSandbox(seed_files={f"{WORK}/grew.txt": "123"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=64, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        async def read_file(*args, **kwargs):
            raise SandboxTransferCapExceeded("grew past the cap")

        monkeypatch.setattr(fake, "read_file", read_file)
        (response,) = asyncio.run(adapter.adownload_files(["grew.txt"]))

        assert response.error == "the file is larger than files_out.max_bytes_per_file"

    def test_a_batch_over_max_files_is_refused_whole(self):
        fake = InProcessSandbox(seed_files={f"{WORK}/a": "1", f"{WORK}/b": "2"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=8, max_total_bytes=8, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.adownload_files(["a", "b"]))

        assert [r.error for r in responses] == [
            "the batch has more files than files_out.max_files allows"
        ] * 2

    def test_the_total_cap_bounds_the_batch_and_a_refused_file_spends_nothing(self):
        fake = InProcessSandbox(
            seed_files={f"{WORK}/a": "1234", f"{WORK}/b": "123", f"{WORK}/c": "12"}
        )
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=6, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.adownload_files(["a", "b", "c"]))

        assert [(r.path, r.error, r.content) for r in responses] == [
            ("a", None, b"1234"),
            ("b", "the batch would exceed files_out.max_total_bytes", None),
            ("c", None, b"12"),
        ]


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

        closed = asyncio.run(adapter.aclose())
        assert closed is True

        assert backend.disposed[before:] == [KEY]
        assert backend.disposed_kinds[before:] == [DEEPAGENTS_KIND]

    def test_the_synchronous_close_is_the_same_call(self):
        adapter, backend = _adapter(InProcessSandbox())
        adapter.execute("true")
        before = len(backend.disposed)
        closed = adapter.close()
        assert closed is True
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
