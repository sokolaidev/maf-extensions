"""Tests for `maf_sandbox.paths` — the guest-path arithmetic kinds and backends share, and
the filesystem path check written on top of it.

These functions are the confinement check itself, so they are pinned directly here rather than
only through the fake that calls them: the cases that matter are the ones where a string looks
contained and is not.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import subprocess
import sys
import tarfile
import warnings
from collections.abc import Sequence
from pathlib import Path

import pytest

from maf_sandbox import EntryKind, SandboxEntry, paths
from maf_sandbox.paths import (
    confine_resolve_guest_delete_path,
    confine_resolve_guest_list_path,
    confine_resolve_guest_path,
    confine_resolve_guest_read_path,
    confine_resolve_guest_write_path,
    guest_path_and_ancestors,
    guest_path_relative_to,
    path_ancestors_are_host_owned,
    refuse_symlinked_ancestors,
    sandbox_entry_from_tar_header,
    stat_by_asking_the_guest,
    stat_by_asking_the_guest_as_root,
    tar_header_from_block,
)

_TAR_BLOCK = 512

_WORK_DIR = "/maf-sandbox/work"


class TestConfineResolveGuestPath:
    def test_a_relative_path_joins_onto_the_working_directory(self):
        assert confine_resolve_guest_path("a.txt", _WORK_DIR) == "/maf-sandbox/work/a.txt"

    def test_a_nested_relative_path_keeps_its_segments(self):
        assert (
            confine_resolve_guest_path("sub/deeper/a.txt", _WORK_DIR)
            == "/maf-sandbox/work/sub/deeper/a.txt"
        )

    def test_an_absolute_path_inside_the_working_directory_is_accepted(self):
        assert (
            confine_resolve_guest_path("/maf-sandbox/work/sub/a.txt", _WORK_DIR)
            == "/maf-sandbox/work/sub/a.txt"
        )

    def test_a_dot_segment_is_normalised_away(self):
        assert confine_resolve_guest_path("./sub/a.txt", _WORK_DIR) == "/maf-sandbox/work/sub/a.txt"

    def test_a_parent_segment_that_stays_inside_is_allowed(self):
        """`..` is not refused on sight — only where it lands is checked."""
        assert confine_resolve_guest_path("sub/../a.txt", _WORK_DIR) == "/maf-sandbox/work/a.txt"

    def test_a_parent_segment_that_escapes_is_refused(self):
        with pytest.raises(ValueError) as caught:
            confine_resolve_guest_path("../etc/passwd", _WORK_DIR)
        assert str(caught.value) == (
            "path '../etc/passwd' resolves outside working directory '/maf-sandbox/work'"
        )

    def test_a_bare_dot_is_the_working_directory_itself(self):
        """The base is inside itself, so this is the one accepted path with nothing below it —
        an emptiness test in place of the `is None` would refuse it."""
        assert confine_resolve_guest_path(".", _WORK_DIR) == "/maf-sandbox/work"

    def test_an_empty_path_is_the_working_directory_itself(self):
        assert confine_resolve_guest_path("", _WORK_DIR) == "/maf-sandbox/work"

    def test_a_working_directory_with_a_trailing_separator_is_normalised_first(self):
        """`working_directory` arrives as whatever the host wrote; unnormalised, `/maf-sandbox/work/` makes
        the base its own non-descendant and `.` resolves outside it."""
        assert confine_resolve_guest_path(".", "/maf-sandbox/work/") == "/maf-sandbox/work"
        assert (
            confine_resolve_guest_path("a.txt", "/maf-sandbox/work/") == "/maf-sandbox/work/a.txt"
        )

    def test_a_working_directory_with_a_dot_segment_is_normalised_first(self):
        assert (
            confine_resolve_guest_path("a.txt", "/maf-sandbox/work/.") == "/maf-sandbox/work/a.txt"
        )

    def test_a_backslash_is_refused_before_anything_is_joined(self):
        """The protocol has one path grammar and `\\` is not a separator in it, whatever the
        host OS — so this is refused rather than normalised into one."""
        with pytest.raises(ValueError) as caught:
            confine_resolve_guest_path("sub\\a.txt", _WORK_DIR)
        assert str(caught.value) == (
            r"path 'sub\\a.txt' contains a backslash, which is not a valid separator"
        )


class TestGuestPathRelativeTo:
    def test_the_base_itself_is_the_empty_string(self):
        """`""`, not `None` and not `"."`: the base is inside itself, and callers key on that
        empty string to mean the working directory's own entry."""
        assert guest_path_relative_to("/maf-sandbox/work", "/maf-sandbox/work") == ""

    def test_a_descendant_is_relative_to_the_base(self):
        assert (
            guest_path_relative_to("/maf-sandbox/work/sub/a.txt", "/maf-sandbox/work")
            == "sub/a.txt"
        )

    def test_a_sibling_sharing_a_string_prefix_is_not_inside(self):
        """`/maf-sandbox/work/sub2` starts with `/maf-sandbox/work/sub`, and a plain prefix test would call it a
        descendant."""
        assert guest_path_relative_to("/maf-sandbox/work/sub2", "/maf-sandbox/work/sub") is None

    def test_a_path_outside_the_base_is_none(self):
        assert guest_path_relative_to("/etc/passwd", "/maf-sandbox/work") is None

    def test_a_base_ending_in_a_separator_does_not_double_it(self):
        assert guest_path_relative_to("/a.txt", "/") == "a.txt"

    @pytest.mark.parametrize(
        ("path", "base"),
        [
            ("/maf-sandbox/work/../etc/passwd", "/maf-sandbox/work"),
            ("/maf-sandbox/work/sub/../../etc", "/maf-sandbox/work"),
            ("/maf-sandbox/work/./../etc", "/maf-sandbox/work"),
        ],
    )
    def test_a_traversal_that_leaves_the_base_is_outside_it(self, path: str, base: str):
        """Both operands are normalised, so a caller using this as its own containment check
        cannot be escaped by a `..` that the string comparison would have carried."""
        assert guest_path_relative_to(path, base) is None

    def test_a_dot_segment_that_stays_inside_is_still_inside(self):
        assert (
            guest_path_relative_to("/maf-sandbox/work/./sub/../a.txt", "/maf-sandbox/work")
            == "a.txt"
        )


class TestConfineResolveGuestWritePath:
    def _run(self, path, *, kinds=None, working_directory=_WORK_DIR):
        kinds = kinds or {}

        async def stat(guest):
            kind = kinds.get(guest)
            return None if kind is None else SandboxEntry(guest, kind, None)

        return asyncio.run(confine_resolve_guest_write_path(stat, path, working_directory))

    def test_a_plain_nested_path_passes(self):
        assert self._run("sub/file.txt") == "/maf-sandbox/work/sub/file.txt"

    def test_parents_that_do_not_exist_yet_pass(self):
        assert self._run("new/deeper/file.txt") == "/maf-sandbox/work/new/deeper/file.txt"

    def test_a_backslash_is_refused(self):
        with pytest.raises(ValueError, match="backslash"):
            self._run(r"sub\file.txt")

    def test_a_path_outside_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            self._run("../outside.txt")

    def test_the_working_directory_itself_is_refused(self):
        with pytest.raises(ValueError, match="working directory"):
            self._run(".")

    def test_a_linked_parent_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            self._run(
                "link/file.txt",
                kinds={
                    "/maf-sandbox": EntryKind.DIRECTORY,
                    "/maf-sandbox/work": EntryKind.DIRECTORY,
                    "/maf-sandbox/work/link": EntryKind.SYMLINK,
                },
            )

    def test_a_non_directory_parent_is_refused(self):
        with pytest.raises(NotADirectoryError):
            self._run(
                "file/child.txt",
                kinds={
                    "/maf-sandbox": EntryKind.DIRECTORY,
                    "/maf-sandbox/work": EntryKind.DIRECTORY,
                    "/maf-sandbox/work/file": EntryKind.FILE,
                },
            )

    def test_a_linked_leaf_is_refused(self):
        with pytest.raises(ValueError, match="is a link"):
            self._run(
                "victim.txt",
                kinds={
                    "/maf-sandbox": EntryKind.DIRECTORY,
                    "/maf-sandbox/work": EntryKind.DIRECTORY,
                    "/maf-sandbox/work/victim.txt": EntryKind.SYMLINK,
                },
            )


#: A work dir whose own ancestors are real, so a case can plant one link and mean it.
_REAL_WORK_DIR = {"/maf-sandbox": EntryKind.DIRECTORY, "/maf-sandbox/work": EntryKind.DIRECTORY}


def _confine(bundle, path, kinds=None, working_directory=_WORK_DIR):
    """Run one bundle over a guest filesystem described as ``{guest path: kind}``."""
    planted = kinds or {}

    async def stat(guest):
        kind = planted.get(guest)
        return None if kind is None else SandboxEntry(guest, kind, None)

    return asyncio.run(bundle(stat, path, working_directory))


class TestConfineResolveGuestReadPath:
    def test_a_plain_nested_path_passes(self):
        assert (
            _confine(confine_resolve_guest_read_path, "sub/a.txt") == "/maf-sandbox/work/sub/a.txt"
        )

    def test_a_path_outside_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            _confine(confine_resolve_guest_read_path, "../a.txt")

    def test_a_linked_ancestor_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            _confine(
                confine_resolve_guest_read_path,
                "link/a.txt",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/link": EntryKind.SYMLINK},
            )

    def test_a_linked_final_component_is_left_to_the_caller(self):
        """What separates this bundle from the others: a stat describes the link, and a read
        refuses it on the kind that comes back."""
        assert (
            _confine(
                confine_resolve_guest_read_path,
                "a.txt",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/a.txt": EntryKind.SYMLINK},
            )
            == "/maf-sandbox/work/a.txt"
        )

    def test_the_working_directory_itself_passes(self):
        """A read owes no refusal there — `stat_file(".")` describes the directory."""
        assert _confine(confine_resolve_guest_read_path, ".", _REAL_WORK_DIR) == _WORK_DIR


class TestConfineResolveGuestListPath:
    def test_a_plain_directory_passes(self):
        assert (
            _confine(confine_resolve_guest_list_path, "sub", _REAL_WORK_DIR)
            == "/maf-sandbox/work/sub"
        )

    def test_a_linked_ancestor_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            _confine(
                confine_resolve_guest_list_path,
                "link/sub",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/link": EntryKind.SYMLINK},
            )

    def test_the_listed_directory_itself_is_refused_when_it_is_a_link(self):
        """What separates this bundle from the read: an enumeration passes through a link as
        readily as a read does, so the directory named here is checked too."""
        with pytest.raises(ValueError, match="real directory"):
            _confine(
                confine_resolve_guest_list_path,
                "link",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/link": EntryKind.SYMLINK},
            )


class TestConfineResolveGuestDeletePath:
    def test_a_plain_nested_path_passes(self):
        assert (
            _confine(confine_resolve_guest_delete_path, "sub/a.txt")
            == "/maf-sandbox/work/sub/a.txt"
        )

    def test_a_path_outside_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            _confine(confine_resolve_guest_delete_path, "../a.txt")

    def test_the_working_directory_itself_is_refused(self):
        with pytest.raises(ValueError, match="working directory"):
            _confine(confine_resolve_guest_delete_path, ".", _REAL_WORK_DIR)

    def test_a_linked_ancestor_is_refused(self):
        with pytest.raises(ValueError, match="real directory"):
            _confine(
                confine_resolve_guest_delete_path,
                "link/a.txt",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/link": EntryKind.SYMLINK},
            )

    def test_the_target_itself_is_never_resolved(self):
        """What separates this bundle from the list: a link named here is the thing being
        unlinked, so resolving it would remove whatever it points at instead."""
        assert (
            _confine(
                confine_resolve_guest_delete_path,
                "link",
                {**_REAL_WORK_DIR, "/maf-sandbox/work/link": EntryKind.SYMLINK},
            )
            == "/maf-sandbox/work/link"
        )


class TestGuestPathAndAncestors:
    def test_the_chain_starts_above_the_working_directory(self):
        """A nested work dir has ancestors the guest can replace, so they are checked too."""
        assert guest_path_and_ancestors("/a/b/maf-sandbox/work", "/a/b/maf-sandbox/work") == (
            "/a",
            "/a/b",
            "/a/b/maf-sandbox",
            "/a/b/maf-sandbox/work",
        )

    def test_a_nested_guest_path_extends_the_chain(self):
        assert guest_path_and_ancestors(
            "/a/b/maf-sandbox/work/out/deeper", "/a/b/maf-sandbox/work"
        ) == (
            "/a",
            "/a/b",
            "/a/b/maf-sandbox",
            "/a/b/maf-sandbox/work",
            "/a/b/maf-sandbox/work/out",
            "/a/b/maf-sandbox/work/out/deeper",
        )

    def test_a_root_working_directory_has_no_ancestors_to_check(self):
        assert guest_path_and_ancestors("/out", "/") == ("/out",)

    def test_a_working_directory_with_a_trailing_separator_is_normalised_first(self):
        assert guest_path_and_ancestors("/a/b/maf-sandbox/work/out", "/a/b/maf-sandbox/work/") == (
            "/a",
            "/a/b",
            "/a/b/maf-sandbox",
            "/a/b/maf-sandbox/work",
            "/a/b/maf-sandbox/work/out",
        )

    def test_a_working_directory_with_a_dot_segment_is_normalised_first(self):
        """Unnormalised, the `.` becomes an ancestor of its own and the real ones are dropped —
        the check would stat everything except what it is for."""
        assert guest_path_and_ancestors("/a/b/maf-sandbox/work/out", "/a/b/maf-sandbox/work/.") == (
            "/a",
            "/a/b",
            "/a/b/maf-sandbox",
            "/a/b/maf-sandbox/work",
            "/a/b/maf-sandbox/work/out",
        )


class TestPathAncestorsAreHostOwned:
    """Every part of the reach rule is load-bearing: the uid, the group write bit, the other
    write bit, and what an empty walk means."""

    def test_a_chain_only_root_can_write_is_host_owned(self):
        assert path_ancestors_are_host_owned(
            {"/a": (0, 0o755), "/a/b": (0, 0o755)},
            empty_means_host_owned=True,
        )

    def test_a_component_owned_by_the_guest_is_not_host_owned(self):
        assert not path_ancestors_are_host_owned(
            {"/a": (0, 0o755), "/a/b": (10001, 0o755)},
            empty_means_host_owned=True,
        )

    def test_a_group_writable_component_is_not_host_owned(self):
        assert not path_ancestors_are_host_owned(
            {"/a": (0, 0o755), "/a/b": (0, 0o775)},
            empty_means_host_owned=True,
        )

    def test_an_other_writable_component_is_not_host_owned(self):
        assert not path_ancestors_are_host_owned(
            {"/a": (0, 0o755), "/a/b": (0, 0o757)},
            empty_means_host_owned=True,
        )

    def test_an_empty_walk_answers_what_the_caller_named(self):
        """Empty is not decided here — a work dir straight under ``/`` has no ancestors to
        stat, but so would a walk that reached nothing, and the caller names that verdict."""
        assert path_ancestors_are_host_owned({}, empty_means_host_owned=True)
        assert not path_ancestors_are_host_owned({}, empty_means_host_owned=False)


class TestRefuseSymlinkedAncestors:
    """The check all three implementations of the pull surface now share.

    Its answers are two different refusals, and telling them apart is the whole point: a link
    is an escape, anything else non-directory is the guest tripping over its own filesystem.
    """

    @staticmethod
    def _stat(kinds: dict[str, EntryKind]):
        """A stat over a literal `{guest path: kind}` map — unconfined and following nothing."""
        statted: list[str] = []

        async def stat(path: str) -> SandboxEntry | None:
            statted.append(path)
            kind = kinds.get(path)
            return None if kind is None else SandboxEntry(path=path, kind=kind, size_bytes=None)

        return stat, statted

    def test_a_chain_of_real_directories_passes(self):
        stat, statted = self._stat(
            {
                "/maf-sandbox": EntryKind.DIRECTORY,
                "/maf-sandbox/work": EntryKind.DIRECTORY,
                "/maf-sandbox/work/out": EntryKind.DIRECTORY,
            }
        )
        asyncio.run(refuse_symlinked_ancestors(stat, "/maf-sandbox/work/out/a.png", _WORK_DIR))
        assert statted == ["/maf-sandbox", "/maf-sandbox/work", "/maf-sandbox/work/out"]

    def test_a_linked_parent_is_an_escape(self):
        stat, _ = self._stat(
            {
                "/maf-sandbox": EntryKind.DIRECTORY,
                "/maf-sandbox/work": EntryKind.DIRECTORY,
                "/maf-sandbox/work/out": EntryKind.SYMLINK,
            }
        )
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(refuse_symlinked_ancestors(stat, "/maf-sandbox/work/out/a.png", _WORK_DIR))

    @pytest.mark.parametrize("kind", [EntryKind.FILE, EntryKind.OTHER])
    def test_any_other_non_directory_parent_is_enotdir(self, kind: EntryKind):
        stat, _ = self._stat(
            {
                "/maf-sandbox": EntryKind.DIRECTORY,
                "/maf-sandbox/work": EntryKind.DIRECTORY,
                "/maf-sandbox/work/out": kind,
            }
        )
        with pytest.raises(NotADirectoryError):
            asyncio.run(refuse_symlinked_ancestors(stat, "/maf-sandbox/work/out/a.png", _WORK_DIR))

    def test_an_ancestor_above_the_working_directory_is_checked_too(self):
        """The `/maf-sandbox -> /` case: a nested work dir has ancestors the guest can replace."""
        stat, _ = self._stat({"/maf-sandbox": EntryKind.SYMLINK})
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(
                refuse_symlinked_ancestors(stat, "/maf-sandbox/work/a.png", "/maf-sandbox/work")
            )

    def test_a_missing_component_ends_the_check_without_refusing(self):
        """A check that finds nothing must not turn a missing output into a confinement failure."""
        stat, statted = self._stat({"/maf-sandbox": EntryKind.DIRECTORY})
        asyncio.run(refuse_symlinked_ancestors(stat, "/maf-sandbox/work/out/a.png", _WORK_DIR))
        assert statted == ["/maf-sandbox", "/maf-sandbox/work"]

    def test_the_path_itself_is_not_checked_by_default(self):
        """Stat is `lstat`-like: the final component is described, not refused."""
        stat, _ = self._stat(
            {
                "/maf-sandbox": EntryKind.DIRECTORY,
                "/maf-sandbox/work": EntryKind.DIRECTORY,
                "/maf-sandbox/work/link": EntryKind.SYMLINK,
            }
        )
        asyncio.run(refuse_symlinked_ancestors(stat, "/maf-sandbox/work/link", _WORK_DIR))

    def test_include_self_checks_it(self):
        """What `list_dir` needs — an enumeration passes through a link as a read does."""
        stat, _ = self._stat(
            {
                "/maf-sandbox": EntryKind.DIRECTORY,
                "/maf-sandbox/work": EntryKind.DIRECTORY,
                "/maf-sandbox/work/link": EntryKind.SYMLINK,
            }
        )
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(
                refuse_symlinked_ancestors(
                    stat, "/maf-sandbox/work/link", _WORK_DIR, include_self=True
                )
            )


class TestTarHeaderHelpers:
    """The tar-header helpers core now owns, and the four branches the two backends used to write."""

    def _block(self, entry: tarfile.TarInfo, data: bytes = b"") -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.addfile(entry, io.BytesIO(data) if data else None)
        return buffer.getvalue()[:_TAR_BLOCK]

    def test_the_header_parses_with_the_pinned_encoding_and_errors(self, monkeypatch):
        arguments: dict[object, object] = {}
        bound = tarfile.TarInfo.frombuf  # the bound classmethod the real call goes through
        descriptor = tarfile.TarInfo.__dict__["frombuf"]

        def spy(block, **kwargs):
            # Keyword-only on the recorder: a positional `frombuf(block, "utf-8", ...)` drift
            # raises here instead of binding silently and reading as pinned.
            arguments.update(kwargs)
            return bound(block, **kwargs)

        # Restored to the descriptor rather than to whatever `getattr` recorded —
        # `monkeypatch` undoes with the bound method, and a class attribute holding that would
        # dispatch subclasses to `TarInfo` for the rest of the session.
        monkeypatch.setattr(tarfile.TarInfo, "frombuf", staticmethod(spy))
        try:
            tar_header_from_block(self._block(tarfile.TarInfo("a.txt")))
        finally:
            monkeypatch.undo()
            tarfile.TarInfo.frombuf = descriptor
        assert arguments == {"encoding": "utf-8", "errors": "surrogateescape"}
        assert tarfile.TarInfo.__dict__["frombuf"] is descriptor

    def test_an_undecodable_name_survives_the_parse(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            entry = tarfile.TarInfo("\udcff\udcfe.txt")
            entry.size = 0
            archive.addfile(entry)
        block = buffer.getvalue()[:_TAR_BLOCK]
        parsed = tar_header_from_block(block)
        assert parsed.name == "\udcff\udcfe.txt"
        assert sandbox_entry_from_tar_header(parsed, "x").kind is EntryKind.FILE

    def test_a_regular_file_keeps_its_size(self):
        entry = tarfile.TarInfo("a.txt")
        entry.size = 7
        parsed = sandbox_entry_from_tar_header(
            tar_header_from_block(self._block(entry, b"body123")), "a.txt"
        )
        assert parsed == SandboxEntry(path="a.txt", kind=EntryKind.FILE, size_bytes=7)

    def test_a_directory_has_no_size(self):
        entry = tarfile.TarInfo("sub/")
        entry.type = tarfile.DIRTYPE
        parsed = sandbox_entry_from_tar_header(tar_header_from_block(self._block(entry)), "sub")
        assert parsed == SandboxEntry(path="sub", kind=EntryKind.DIRECTORY, size_bytes=None)

    def test_a_symlink_has_no_size(self):
        entry = tarfile.TarInfo("out")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc"
        parsed = sandbox_entry_from_tar_header(tar_header_from_block(self._block(entry)), "out")
        assert parsed == SandboxEntry(path="out", kind=EntryKind.SYMLINK, size_bytes=None)

    def test_a_hard_link_is_other_with_no_size(self):
        entry = tarfile.TarInfo("dup")
        entry.type = tarfile.LNKTYPE
        entry.linkname = "a.txt"
        parsed = sandbox_entry_from_tar_header(tar_header_from_block(self._block(entry)), "dup")
        assert parsed == SandboxEntry(path="dup", kind=EntryKind.OTHER, size_bytes=None)

    def test_a_fifo_is_other_with_no_size(self):
        entry = tarfile.TarInfo("pipe")
        entry.type = tarfile.FIFOTYPE
        parsed = sandbox_entry_from_tar_header(tar_header_from_block(self._block(entry)), "pipe")
        assert parsed == SandboxEntry(path="pipe", kind=EntryKind.OTHER, size_bytes=None)

    def test_an_extended_header_block_is_other(self):
        """A GNU long-name header (`L`) is metadata ahead of an entry, not an entry: classify
        it `OTHER` with no size, so a caller refuses rather than follows it. Reaching long
        names is the pull surface's question, not the classifier's."""
        long_name = tarfile.TarInfo("x" * 120)
        long_name.type = tarfile.GNUTYPE_LONGNAME
        parsed = sandbox_entry_from_tar_header(long_name, "a.txt")
        assert parsed == SandboxEntry(path="a.txt", kind=EntryKind.OTHER, size_bytes=None)

    def test_the_classifier_takes_the_parsed_header_docker_already_holds(self):
        info = tarfile.TarInfo("kept.txt")
        info.size = 3
        assert sandbox_entry_from_tar_header(info, "kept.txt").size_bytes == 3

    def test_both_names_are_exported(self):
        assert "tar_header_from_block" in paths.__all__
        assert "sandbox_entry_from_tar_header" in paths.__all__


class _Guest:
    """A guest answering ``test``, from a planted table and recording what it was asked.

    Keyed ``(flag, path)``, and anything unplanted answers false — which is what an absent path
    and one under a directory the asker cannot search both look like, and is the whole of what
    the two variants differ about.
    """

    def __init__(self, answers: dict[tuple[str, str], int] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str]) -> int:
        self.asked.append(tuple(argv))
        _, flag, path = argv
        return self.answers.get((flag, path), 1)


class TestStatByAskingTheGuestAsRoot:
    """The raised variant: reach enough that a path nothing answers for is genuinely absent."""

    def test_a_link_is_asked_about_before_anything_follows_one(self):
        """The ordering the whole helper exists for. This guest answers yes to both `-L` and
        `-d`, which is what a link to a directory does; asking `-d` first would classify it a
        real directory and the escape would never appear."""
        guest = _Guest({("-L", "/w/out"): 0, ("-d", "/w/out"): 0})
        entry = asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/out", "out"))
        assert entry == SandboxEntry(path="out", kind=EntryKind.SYMLINK, size_bytes=None)
        assert guest.asked == [("test", "-L", "/w/out")]

    def test_a_directory_comes_back_as_a_directory(self):
        guest = _Guest({("-d", "/w/sub"): 0})
        entry = asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/sub", "sub"))
        assert entry == SandboxEntry(path="sub", kind=EntryKind.DIRECTORY, size_bytes=None)

    def test_a_regular_file_comes_back_without_a_size(self):
        """`test` reports no size. That is enough for the filesystem path check, which reads
        only the kind, and is why this is not a `stat_file`."""
        guest = _Guest({("-f", "/w/a.txt"): 0})
        entry = asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/a.txt", "a.txt"))
        assert entry == SandboxEntry(path="a.txt", kind=EntryKind.FILE, size_bytes=None)

    def test_an_entry_no_shape_flag_matches_is_other_rather_than_absent(self):
        """A fifo, a socket and a device node answer none of the three shape flags. `-e` is what
        keeps one from reading as an absent component, which would end the check."""
        guest = _Guest({("-e", "/w/pipe"): 0})
        entry = asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/pipe", "pipe"))
        assert entry == SandboxEntry(path="pipe", kind=EntryKind.OTHER, size_bytes=None)

    def test_a_path_nothing_answers_for_is_absent(self):
        guest = _Guest()
        assert asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/gone", "gone")) is None

    def test_the_parents_search_bit_is_never_asked_about(self):
        """Root searches every directory, so the disambiguation the other variant owes is not
        one this variant has to pay for."""
        guest = _Guest()
        asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/gone", "gone"))
        assert [argv[1] for argv in guest.asked] == ["-L", "-d", "-f", "-e"]

    def test_the_argv_is_test_one_flag_and_the_path(self):
        """One flag and one operand, so no shell is needed and there is nothing to quote."""
        guest = _Guest()
        asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/a b.txt", "a b.txt"))
        assert guest.asked[0] == ("test", "-L", "/w/a b.txt")

    def test_an_exit_above_false_raises_rather_than_reading_as_a_no(self):
        """126 is the engine refusing to start the command and 127 is a missing `test`. Either
        read as a no ends the check over a path that was never classified."""
        guest = _Guest({("-L", "/w/a.txt"): 126})
        with pytest.raises(RuntimeError, match="exit 126"):
            asyncio.run(stat_by_asking_the_guest_as_root(guest, "/w/a.txt", "a.txt"))


class TestStatByAskingTheGuest:
    """The guest's own principal: less reach, and the ambiguity that costs is stated not guessed."""

    def test_a_planted_kind_answers_as_the_raised_variant_does(self):
        guest = _Guest({("-L", "/w/out"): 0})
        entry = asyncio.run(stat_by_asking_the_guest(guest, "/w/out", "out"))
        assert entry == SandboxEntry(path="out", kind=EntryKind.SYMLINK, size_bytes=None)

    def test_a_full_miss_under_a_searchable_parent_is_absent(self):
        guest = _Guest({("-e", "/w"): 0, ("-x", "/w"): 0})
        assert asyncio.run(stat_by_asking_the_guest(guest, "/w/gone", "gone")) is None

    def test_a_full_miss_under_a_parent_it_cannot_search_is_refused(self):
        """Absent and invisible answer identically here, and absent would end the check. The
        refusal is what stops a path that is really there from passing as one that is not."""
        guest = _Guest({("-e", "/w"): 0})
        with pytest.raises(PermissionError, match="cannot be told apart"):
            asyncio.run(stat_by_asking_the_guest(guest, "/w/gone", "gone"))

    def test_the_parent_is_asked_about_only_after_every_probe_missed(self):
        guest = _Guest({("-d", "/w/sub"): 0})
        asyncio.run(stat_by_asking_the_guest(guest, "/w/sub", "sub"))
        assert [argv[1] for argv in guest.asked] == ["-L", "-d"]

    def test_a_parent_that_is_not_there_yet_is_not_a_refusal(self):
        """`write_file` creates parents, so a path under a chain that does not exist yet is
        ordinary — `test_parents_that_do_not_exist_yet_pass` is the contract. A missing parent
        is not itself an answer, so this climbs to the nearest ancestor that is there and asks
        *its* search bit."""
        guest = _Guest({("-e", "/w"): 0, ("-x", "/w"): 0})
        assert asyncio.run(stat_by_asking_the_guest(guest, "/w/new/deep/f.txt", "…")) is None

    def test_the_ancestor_that_blocks_the_view_is_the_one_named(self):
        """`/w/new` does not exist and `/w` cannot be searched, so nothing below `/w` is
        knowable and the refusal names `/w` rather than the immediate parent."""
        guest = _Guest({("-e", "/w"): 0})
        with pytest.raises(PermissionError, match="'/w'"):
            asyncio.run(stat_by_asking_the_guest(guest, "/w/new/f.txt", "…"))

    def test_a_root_that_answers_nothing_is_refused_rather_than_read_as_absent(self):
        """Nothing above `/` to climb to, so its own search bit is the last question. A `test`
        that answers no to that is broken rather than reporting an absent root, and reading it
        as absence would end the check."""
        guest = _Guest()
        with pytest.raises(PermissionError, match="cannot be told apart"):
            asyncio.run(stat_by_asking_the_guest(guest, "/", "/"))


class TestTheGuestSideStatUnderTheCheck:
    def test_a_write_into_a_chain_that_does_not_exist_yet_is_confined_and_allowed(self):
        """The whole bundle over the guest's own principal, which is where the absent-parent
        reading actually bites: the check ends at the first missing component, then the write
        bundle stats the leaf, whose parent is missing too."""
        guest = _Guest(
            {
                ("-d", "/maf-sandbox"): 0,
                ("-e", "/maf-sandbox"): 0,
                ("-x", "/maf-sandbox"): 0,
                ("-d", _WORK_DIR): 0,
                ("-e", _WORK_DIR): 0,
                ("-x", _WORK_DIR): 0,
            }
        )
        resolved = asyncio.run(
            confine_resolve_guest_write_path(
                lambda directory: stat_by_asking_the_guest(guest, directory, directory),
                "new/deeper/file.txt",
                _WORK_DIR,
            )
        )
        assert resolved == "/maf-sandbox/work/new/deeper/file.txt"

    def test_a_linked_ancestor_is_refused(self):
        guest = _Guest({("-d", "/maf-sandbox"): 0, ("-L", "/maf-sandbox/work"): 0})
        with pytest.raises(ValueError, match="is a link rather than a real directory"):
            asyncio.run(
                refuse_symlinked_ancestors(
                    lambda directory: stat_by_asking_the_guest_as_root(guest, directory, directory),
                    "/maf-sandbox/work/a.txt",
                    _WORK_DIR,
                )
            )

    def test_both_names_are_exported(self):
        assert "stat_by_asking_the_guest" in paths.__all__
        assert "stat_by_asking_the_guest_as_root" in paths.__all__


class TestTheNamesTheseHadBefore:
    """Each spelling from before the rename warns when *called* and delegates to its replacement.

    Not on lookup: importing one must stay silent, or the three shipped backends fail under
    ``-W error``.
    """

    RENAMED = [
        ("confine_guest_path", "confine_resolve_guest_path"),
        ("confine_guest_write_path", "confine_resolve_guest_write_path"),
        ("guest_directory_chain", "guest_path_and_ancestors"),
        ("refuse_symlinked_parents", "refuse_symlinked_ancestors"),
    ]

    def test_the_sync_pair_warns_and_delegates(self):
        with pytest.warns(DeprecationWarning, match="confine_resolve_guest_path"):
            confined = paths.confine_guest_path("out/a.png", _WORK_DIR)
        assert confined == paths.confine_resolve_guest_path("out/a.png", _WORK_DIR)

        with pytest.warns(DeprecationWarning, match="guest_path_and_ancestors"):
            returned = paths.guest_directory_chain("/maf-sandbox/work/out", _WORK_DIR)
        assert returned == paths.guest_path_and_ancestors("/maf-sandbox/work/out", _WORK_DIR)

    def test_the_async_pair_warns_and_delegates(self):
        stat, _ = TestRefuseSymlinkedAncestors._stat(
            {"/maf-sandbox": EntryKind.DIRECTORY, "/maf-sandbox/work": EntryKind.DIRECTORY}
        )

        with pytest.warns(DeprecationWarning, match="refuse_symlinked_ancestors"):
            asyncio.run(paths.refuse_symlinked_parents(stat, "/maf-sandbox/work/a.png", _WORK_DIR))

        with pytest.warns(DeprecationWarning, match="confine_resolve_guest_write_path"):
            written = asyncio.run(paths.confine_guest_write_path(stat, "a.png", _WORK_DIR))
        assert written == "/maf-sandbox/work/a.png"

    def test_the_warning_names_the_caller_and_not_asyncio(self):
        """The warning names this file, not `asyncio/events.py`.

        `confine_guest_write_path` gives the reason the shims are sync and return a coroutine.
        """
        stat, _ = TestRefuseSymlinkedAncestors._stat(
            {"/maf-sandbox": EntryKind.DIRECTORY, "/maf-sandbox/work": EntryKind.DIRECTORY}
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(paths.refuse_symlinked_parents(stat, "/maf-sandbox/work/a.png", _WORK_DIR))

        assert Path(caught[0].filename).name == Path(__file__).name

    def test_the_async_shims_still_answer_iscoroutinefunction(self):
        """Both spellings were `async def`, so both must keep reading as coroutine functions.

        A sync shim returning a coroutine is invisible to `await` and visible to
        `inspect.iscoroutinefunction`, which is what a caller that dispatches on it would stop
        awaiting. `inspect.markcoroutinefunction` restores the answer the old spelling gave.
        """
        for old, new in (
            (paths.confine_guest_write_path, paths.confine_resolve_guest_write_path),
            (paths.refuse_symlinked_parents, paths.refuse_symlinked_ancestors),
        ):
            assert inspect.iscoroutinefunction(new)
            assert inspect.iscoroutinefunction(old), f"{old.__name__} reads as synchronous"

    def test_importing_the_old_spelling_does_not_warn(self):
        """The three shipped backends import these; warning here fails them under `-W error`."""
        source = "from maf_sandbox.paths import refuse_symlinked_parents, confine_guest_path"
        completed = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-c", source],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    @pytest.mark.parametrize(("old", "new"), RENAMED)
    def test_both_spellings_stay_importable_for_the_cycle(self, old: str, new: str):
        """Keeping the old name means keeping it in `__all__` too, until the removal minor."""
        assert old in paths.__all__
        assert new in paths.__all__
