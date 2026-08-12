"""Tests for `maf_sandbox.testing`.

A fake nobody outside its own package tests is a fake that drifts silently — this pins the
public testing surface's own contract, separately from the router and bicep-kind suites that
consume it.
"""

from __future__ import annotations

import asyncio
import shlex

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    WorkspaceContext,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandbox, InProcessSandboxBackend

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="test")

#: A read bound far above anything these fixtures store — for the calls where the bound is not
#: what is under test. `TestInProcessSandboxReadBound` is where it is.
_AMPLE = 1024


class TestInProcessSandboxExec:
    def test_write_file_records_content_by_path(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "hello"))
        assert sandbox.files == {"/work/a.txt": "hello"}

    def test_a_string_command_is_recorded_as_given(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.exec("echo hi", working_directory="/work", timeout=5))
        assert sandbox.commands == [("echo hi", "/work", 5)]

    def test_default_stdout_is_empty_when_not_configured(self):
        sandbox = InProcessSandbox()
        result = asyncio.run(sandbox.exec("anything", working_directory="/work", timeout=5))
        assert result.stdout == ""

    def test_default_stdout_is_the_callers_choice(self):
        """The bicep kind's tests want an empty SARIF document here — this proves it is
        configurable rather than baked into the generic fake."""
        sandbox = InProcessSandbox(default_stdout="EMPTY-SARIF")
        result = asyncio.run(sandbox.exec("bicep build x", working_directory="/work", timeout=5))
        assert result.stdout == "EMPTY-SARIF"

    def test_the_first_matching_marker_scripts_the_output(self):
        sandbox = InProcessSandbox(outputs={"lint": "LINT-OUT", "build": "BUILD-OUT"})
        result = asyncio.run(
            sandbox.exec("bicep lint main.bicep", working_directory="/work", timeout=5)
        )
        assert result.stdout == "LINT-OUT"

    def test_raises_propagates_instead_of_returning_a_result(self):
        sandbox = InProcessSandbox(raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(sandbox.exec("x", working_directory="/work", timeout=5))

    def test_exec_returns_an_exec_result(self):
        sandbox = InProcessSandbox()
        result = asyncio.run(sandbox.exec("x", working_directory="/work", timeout=5))
        assert isinstance(result, ExecResult)


class TestInProcessSandboxArgvHardening:
    """A sequence must be quoted before recording — and the quoting must be reversible.

    ``shlex.split`` of the recorded command must recover exactly the original argv: that is
    the property that proves an element containing a shell metacharacter cannot be
    re-interpreted as a second token or a second command once it reaches a real shell.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["echo", "a b"],
            ["echo", "a; rm -rf /"],
            ["echo", "$(id)"],
            ["echo", "`id`"],
            ["echo", "it's mine"],
            ["echo", 'say "hi"'],
            ["bicep", "build", "/acas/work/r1/main.bicep", "--diagnostics-format", "sarif"],
        ],
    )
    def test_a_sequence_round_trips_through_shlex_split(self, argv):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.exec(argv, working_directory="/work", timeout=5))
        (recorded,) = [command for command, _, _ in sandbox.commands]
        assert recorded == shlex.join(argv)
        assert shlex.split(recorded) == argv

    def test_marker_matching_still_works_against_a_joined_sequence(self):
        """A marker written against a string command must still match the joined form."""
        sandbox = InProcessSandbox(outputs={"bicep lint": "LINT-OUT"})
        result = asyncio.run(
            sandbox.exec(["bicep", "lint", "main.bicep"], working_directory="/work", timeout=5)
        )
        assert result.stdout == "LINT-OUT"


class TestInProcessSandboxBackend:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(InProcessSandboxBackend(), SandboxBackend)

    def test_defaults_to_in_process_name_and_process_isolation(self):
        backend = InProcessSandboxBackend()
        assert backend.name == "in-process"
        assert backend.isolation == Isolation.PROCESS

    def test_name_and_isolation_are_configurable(self):
        backend = InProcessSandboxBackend(name="fake", isolation=Isolation.VM)
        assert backend.name == "fake"
        assert backend.isolation == Isolation.VM

    def test_egress_defaults_to_allowlist_so_a_workload_attaches(self):
        """Not `CLOSED`, or every consumer's offline test becomes a test of the refusal."""
        assert InProcessSandboxBackend().egress == Egress.ALLOWLIST

    def test_egress_is_configurable(self):
        assert InProcessSandboxBackend(egress=Egress.UNRESTRICTED).egress == Egress.UNRESTRICTED

    def test_capabilities_default_to_what_every_sandbox_owes(self):
        """`write_file` and `exec` — the two the `Sandbox` protocol already obligates."""
        assert InProcessSandboxBackend().capabilities == DEFAULT_CAPABILITIES

    def test_capabilities_are_configurable(self):
        """A kind's tests need a backend that claims more, and one that claims less."""
        backend = InProcessSandboxBackend(capabilities=frozenset({Capability.RUN_CODE}))
        assert backend.capabilities == frozenset({Capability.RUN_CODE})

    def test_acquire_records_the_key_and_spec_and_returns_the_sandbox(self):
        sandbox = InProcessSandbox()
        backend = InProcessSandboxBackend(sandbox)
        result = asyncio.run(backend.acquire(_KEY, _SPEC))
        assert result is sandbox
        assert backend.keys == [_KEY]
        assert backend.specs == [_SPEC]

    def test_a_default_sandbox_is_created_when_none_is_given(self):
        backend = InProcessSandboxBackend()
        assert isinstance(backend.sandbox, InProcessSandbox)

    def test_acquire_error_is_raised_and_nothing_is_recorded(self):
        backend = InProcessSandboxBackend(acquire_error=RuntimeError("unavailable"))
        with pytest.raises(RuntimeError, match="unavailable"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert backend.keys == []
        assert backend.specs == []

    def test_dispose_records_the_key(self):
        backend = InProcessSandboxBackend()
        asyncio.run(backend.dispose(_KEY))
        assert backend.disposed == [_KEY]

    def test_dispose_scope_records_and_returns_the_purge_count(self):
        backend = InProcessSandboxBackend()
        result = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert result == 1
        assert backend.purged == [("scope-a", "thread-1")]

    def test_purge_count_is_settable(self):
        backend = InProcessSandboxBackend()
        backend.purge_count = 3
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 3


class TestInMemoryStore:
    def test_read_returns_the_content(self):
        store = InMemoryStore({"main.bicep": "param x string"})
        assert asyncio.run(store.read("main.bicep")) == "param x string"

    def test_read_returns_none_for_a_missing_file_rather_than_raising(self):
        """Mirrors `AgentFileStore.read`'s real contract — a miss is data, not an exception."""
        store = InMemoryStore({})
        assert asyncio.run(store.read("missing.bicep")) is None

    def test_list_returns_every_name(self):
        store = InMemoryStore({"a.bicep": "x", "b.bicep": "y"})
        assert sorted(asyncio.run(store.list())) == ["a.bicep", "b.bicep"]

    def test_construction_copies_the_callers_dict(self):
        source = {"a.bicep": "x"}
        store = InMemoryStore(source)
        store.files["b.bicep"] = "y"
        assert "b.bicep" not in source

    def test_the_unbound_list_method_matches_workspace_context_list_files(self):
        """`list_files=InMemoryStore.list` must work directly, with no wrapper."""
        store = InMemoryStore({"a.bicep": "x", "b.bicep": "y"})
        context = WorkspaceContext(
            current_scope=lambda: "scope-a",
            current_thread_id=lambda: "thread-1",
            list_files=InMemoryStore.list,
        )
        assert sorted(asyncio.run(context.list_files(store))) == ["a.bicep", "b.bicep"]


class TestInProcessSandboxSatisfiesTheProtocol:
    def test_satisfies_the_sandbox_protocol(self):
        assert isinstance(InProcessSandbox(), Sandbox)


class TestInProcessSandboxWriteFileIsBytesBacked:
    def test_str_content_is_utf8_encoded_on_the_way_in(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/greeting.txt", "héllo"))
        result = asyncio.run(
            sandbox.read_file("greeting.txt", working_directory="/work", max_bytes=_AMPLE)
        )
        assert result == "héllo".encode()

    def test_bytes_content_round_trips_exactly(self):
        sandbox = InProcessSandbox()
        payload = b"\x89PNG\r\n\x1a\n\x00\x01"
        asyncio.run(sandbox.write_file("/work/out.png", payload))
        result = asyncio.run(
            sandbox.read_file("out.png", working_directory="/work", max_bytes=_AMPLE)
        )
        assert result == payload

    def test_files_stays_a_str_dict_for_callers_written_against_the_old_shape(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "hello"))
        assert sandbox.files == {"/work/a.txt": "hello"}


class TestFilesIsAReadOnlyViewOfTheByteStore:
    """`files` is computed, so a write to it would be discarded — and a test seeding through
    it would pass while asserting nothing. It refuses instead, and names where to write."""

    def test_a_write_raises_rather_than_vanishing(self):
        sandbox = InProcessSandbox()
        with pytest.raises(TypeError):
            sandbox.files["/work/a.txt"] = "hello"  # type: ignore[index]

    def test_the_byte_store_is_public_and_is_what_a_caller_writes(self):
        sandbox = InProcessSandbox()
        sandbox.contents["/work/out.png"] = b"\x89PNG"
        assert asyncio.run(sandbox.read_file("out.png", working_directory="/work", max_bytes=4))

    def test_reading_it_raises_on_content_that_is_not_text(self):
        """Strict on purpose, and documented: asking for text that was never stored is worth
        an error rather than a string of replacement characters. `contents` has the bytes."""
        sandbox = InProcessSandbox(seed_files={"/work/out.png": b"\x89PNG"})
        with pytest.raises(UnicodeDecodeError):
            sandbox.files  # noqa: B018
        assert sandbox.contents == {"/work/out.png": b"\x89PNG"}


class TestInProcessSandboxReadBound:
    """`read_file`'s `max_bytes` is a refusal, never a truncation — the protocol's rule."""

    def test_a_file_at_the_bound_is_served(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.bin": b"0123"})
        assert asyncio.run(sandbox.read_file("a.bin", working_directory="/work", max_bytes=4))

    def test_a_file_over_the_bound_is_refused_and_nothing_is_returned(self):
        """A short read reported as success is an artifact the host cannot tell from a whole
        one, so there is no truncating branch to reach."""
        sandbox = InProcessSandbox(seed_files={"/work/a.bin": b"0123"})
        with pytest.raises(SandboxTransferCapExceeded, match="a.bin"):
            asyncio.run(sandbox.read_file("a.bin", working_directory="/work", max_bytes=3))


class TestInProcessSandboxSeedFiles:
    """`seed_files=` populates the read surface; `outputs=` scripts exec's stdout — distinct
    names so a kind's tests never need both in one expression."""

    def test_a_str_seed_is_utf8_encoded_like_write_file(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.txt": "seeded"})
        result = asyncio.run(
            sandbox.read_file("a.txt", working_directory="/work", max_bytes=_AMPLE)
        )
        assert result == b"seeded"

    def test_a_bytes_seed_is_stored_as_given(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.bin": b"\x00\x01"})
        result = asyncio.run(
            sandbox.read_file("a.bin", working_directory="/work", max_bytes=_AMPLE)
        )
        assert result == b"\x00\x01"

    def test_seed_files_and_outputs_are_independent(self):
        sandbox = InProcessSandbox(outputs={"echo": "ECHO-OUT"}, seed_files={"/work/a.txt": "x"})
        result = asyncio.run(sandbox.exec("echo hi", working_directory="/work", timeout=5))
        assert result.stdout == "ECHO-OUT"
        assert (
            asyncio.run(sandbox.read_file("a.txt", working_directory="/work", max_bytes=_AMPLE))
            == b"x"
        )

    def test_entry_kind_other_seeds_a_non_regular_path(self):
        sandbox = InProcessSandbox(seed_files={"/work/link": EntryKind.OTHER})
        entry = asyncio.run(sandbox.stat_file("link", working_directory="/work"))
        assert entry == SandboxEntry(path="link", kind=EntryKind.OTHER, size_bytes=None)


class TestInProcessSandboxStatFile:
    def test_stats_a_regular_file(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "hello"))
        entry = asyncio.run(sandbox.stat_file("a.txt", working_directory="/work"))
        assert entry == SandboxEntry(path="a.txt", kind=EntryKind.FILE, size_bytes=5)

    def test_size_bytes_counts_bytes_not_characters(self):
        """`é` is one character and two UTF-8 bytes — the cap this feeds must see the bytes."""
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "é"))
        entry = asyncio.run(sandbox.stat_file("a.txt", working_directory="/work"))
        assert entry is not None
        assert entry.size_bytes == 2

    def test_returns_none_for_nothing_there(self):
        sandbox = InProcessSandbox()
        assert asyncio.run(sandbox.stat_file("missing.txt", working_directory="/work")) is None

    def test_stats_an_implied_directory(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/sub/a.txt", "x"))
        entry = asyncio.run(sandbox.stat_file("sub", working_directory="/work"))
        assert entry == SandboxEntry(path="sub", kind=EntryKind.DIRECTORY, size_bytes=None)


class TestInProcessSandboxReadFile:
    def test_reads_written_bytes(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "hello"))
        assert (
            asyncio.run(sandbox.read_file("a.txt", working_directory="/work", max_bytes=_AMPLE))
            == b"hello"
        )

    def test_refuses_a_missing_file(self):
        sandbox = InProcessSandbox()
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                sandbox.read_file("missing.txt", working_directory="/work", max_bytes=_AMPLE)
            )

    def test_refuses_a_directory(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/sub/a.txt", "x"))
        with pytest.raises(IsADirectoryError):
            asyncio.run(sandbox.read_file("sub", working_directory="/work", max_bytes=_AMPLE))

    def test_refuses_a_non_regular_entry(self):
        """The confinement rule that matters: refused whether or not a real target would have
        resolved somewhere legitimate — this fake models that by never storing content for it."""
        sandbox = InProcessSandbox(seed_files={"/work/out/link": EntryKind.OTHER})
        with pytest.raises(OSError):
            asyncio.run(sandbox.read_file("out/link", working_directory="/work", max_bytes=_AMPLE))


class TestInProcessSandboxListDir:
    def test_lists_files_and_collapses_a_subtree_into_one_directory_entry(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "1"))
        asyncio.run(sandbox.write_file("/work/sub/nested/b.txt", "22"))
        entries = asyncio.run(sandbox.list_dir(".", working_directory="/work"))
        assert set(entries) == {
            SandboxEntry(path="a.txt", kind=EntryKind.FILE, size_bytes=1),
            SandboxEntry(path="sub", kind=EntryKind.DIRECTORY, size_bytes=None),
        }

    def test_lists_only_the_immediate_children_of_a_subdirectory(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/sub/b.txt", "22"))
        asyncio.run(sandbox.write_file("/work/sub/nested/c.txt", "3"))
        entries = asyncio.run(sandbox.list_dir("sub", working_directory="/work"))
        assert set(entries) == {
            SandboxEntry(path="sub/b.txt", kind=EntryKind.FILE, size_bytes=2),
            SandboxEntry(path="sub/nested", kind=EntryKind.DIRECTORY, size_bytes=None),
        }

    def test_a_non_regular_entry_lists_as_other(self):
        sandbox = InProcessSandbox(seed_files={"/work/link": EntryKind.OTHER})
        entries = asyncio.run(sandbox.list_dir(".", working_directory="/work"))
        assert entries == (SandboxEntry(path="link", kind=EntryKind.OTHER, size_bytes=None),)

    def test_an_empty_directory_lists_as_no_entries(self):
        sandbox = InProcessSandbox()
        assert asyncio.run(sandbox.list_dir(".", working_directory="/work")) == ()


class TestInProcessSandboxConfinement:
    """`stat_file`, `read_file` and `list_dir` share one resolver — exercised through
    `stat_file`, since a refusal raises before any kind-specific behaviour diverges."""

    def test_a_backslash_in_the_path_is_refused(self):
        sandbox = InProcessSandbox()
        with pytest.raises(ValueError, match="backslash"):
            asyncio.run(sandbox.stat_file("a\\b.txt", working_directory="/work"))

    def test_a_resolved_path_outside_the_working_directory_is_refused(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/etc/passwd", "root:x"))
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(sandbox.stat_file("../../etc/passwd", working_directory="/work/sub"))

    def test_a_same_prefix_sibling_directory_is_not_treated_as_a_descendant(self):
        """`/work/sub2` must not read as inside `/work/sub` because the strings share a prefix."""
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/sub2/a.txt", "x"))
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(sandbox.stat_file("../sub2/a.txt", working_directory="/work/sub"))

    def test_list_dir_applies_the_same_confinement(self):
        sandbox = InProcessSandbox()
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(sandbox.list_dir("..", working_directory="/work"))


class TestInProcessSandboxWalksTheComponents:
    """The rule a lexical check cannot see: a path whose *parent* is a link leaves the work dir.

    A fake that cannot express the scenario lets every kind's tests pass it without exercising
    it, and cannot host the conformance suite either (#142).
    """

    @staticmethod
    def _sandbox() -> InProcessSandbox:
        return InProcessSandbox(
            seed_files={
                "/work/real.txt": "artifact",
                "/work/link-dir": EntryKind.SYMLINK,
                "/work/link-dir/hostname": "a-real-host\n",
                "/work/pipe": EntryKind.OTHER,
            }
        )

    def test_a_stat_through_a_linked_parent_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(self._sandbox().stat_file("link-dir/hostname", working_directory="/work"))

    def test_a_read_through_a_linked_parent_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(
                self._sandbox().read_file(
                    "link-dir/hostname", working_directory="/work", max_bytes=_AMPLE
                )
            )

    def test_a_listing_of_a_linked_directory_is_refused(self):
        """`list_dir` walks one deeper than the other two: enumeration follows a link as well."""
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(self._sandbox().list_dir("link-dir", working_directory="/work"))

    def test_a_linked_working_directory_is_refused_too(self):
        """The walk starts above the working directory, not at it — the `/acas -> /` case."""
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(self._sandbox().stat_file("hostname", working_directory="/work/link-dir"))

    def test_a_path_through_a_regular_file_is_not_reported_as_an_escape(self):
        """`ENOTDIR` is not a confinement failure, and only a link makes it one."""
        with pytest.raises(NotADirectoryError):
            asyncio.run(self._sandbox().stat_file("real.txt/child", working_directory="/work"))

    def test_a_path_through_a_non_regular_entry_is_not_an_escape_either(self):
        with pytest.raises(NotADirectoryError):
            asyncio.run(self._sandbox().stat_file("pipe/child", working_directory="/work"))

    def test_a_final_component_link_is_described_rather_than_refused(self):
        entry = asyncio.run(self._sandbox().stat_file("link-dir", working_directory="/work"))
        assert entry == SandboxEntry(path="link-dir", kind=EntryKind.SYMLINK, size_bytes=None)

    def test_a_link_is_listed_rather_than_hidden(self):
        entries = asyncio.run(self._sandbox().list_dir(".", working_directory="/work"))
        assert SandboxEntry(path="link-dir", kind=EntryKind.SYMLINK, size_bytes=None) in entries

    def test_a_link_is_never_read(self):
        with pytest.raises(OSError):
            asyncio.run(
                self._sandbox().read_file("link-dir", working_directory="/work", max_bytes=_AMPLE)
            )

    def test_a_missing_component_leaves_the_refusal_to_the_call_itself(self):
        """A walk that finds nothing must not turn a missing output into a confinement failure."""
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                self._sandbox().read_file(
                    "gone/output", working_directory="/work", max_bytes=_AMPLE
                )
            )

    def test_the_planting_surface_is_public(self):
        """`symlinks` and `non_regular` are stores a test writes to, like `contents`."""
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/.keep", ""))
        sandbox.symlinks.add("/work/late")
        entry = asyncio.run(sandbox.stat_file("late", working_directory="/work"))
        assert entry is not None and entry.kind is EntryKind.SYMLINK


class TestInProcessSandboxBackendLimits:
    def test_limits_default_to_default_sandbox_limits(self):
        assert InProcessSandboxBackend().limits == DEFAULT_SANDBOX_LIMITS

    def test_limits_are_configurable(self):
        custom = SandboxLimits(
            files_out=TransferLimits(max_bytes_per_file=1, max_total_bytes=1, max_files=1)
        )
        assert InProcessSandboxBackend(limits=custom).limits == custom
