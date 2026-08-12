"""Tests for `maf_sandbox.paths` — the guest-path arithmetic kinds and backends share, and
the component walk written on top of it.

These functions are the confinement check itself, so they are pinned directly here rather than
only through the fake that calls them: the cases that matter are the ones where a string looks
contained and is not.
"""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import EntryKind, SandboxEntry
from maf_sandbox.paths import (
    confine_guest_path,
    guest_directory_chain,
    guest_path_relative_to,
    refuse_symlinked_parents,
)

_WORK_DIR = "/work"


class TestConfineGuestPath:
    def test_a_relative_path_joins_onto_the_working_directory(self):
        assert confine_guest_path("a.txt", _WORK_DIR) == "/work/a.txt"

    def test_a_nested_relative_path_keeps_its_segments(self):
        assert confine_guest_path("sub/deeper/a.txt", _WORK_DIR) == "/work/sub/deeper/a.txt"

    def test_an_absolute_path_inside_the_working_directory_is_accepted(self):
        assert confine_guest_path("/work/sub/a.txt", _WORK_DIR) == "/work/sub/a.txt"

    def test_a_dot_segment_is_normalised_away(self):
        assert confine_guest_path("./sub/a.txt", _WORK_DIR) == "/work/sub/a.txt"

    def test_a_parent_segment_that_stays_inside_is_allowed(self):
        """`..` is not refused on sight — only where it lands is checked."""
        assert confine_guest_path("sub/../a.txt", _WORK_DIR) == "/work/a.txt"

    def test_a_parent_segment_that_escapes_is_refused(self):
        with pytest.raises(ValueError) as caught:
            confine_guest_path("../etc/passwd", _WORK_DIR)
        assert str(caught.value) == (
            "path '../etc/passwd' resolves outside working directory '/work'"
        )

    def test_a_bare_dot_is_the_working_directory_itself(self):
        """The base is inside itself, so this is the one accepted path with nothing below it —
        an emptiness test in place of the `is None` would refuse it."""
        assert confine_guest_path(".", _WORK_DIR) == "/work"

    def test_an_empty_path_is_the_working_directory_itself(self):
        assert confine_guest_path("", _WORK_DIR) == "/work"

    def test_a_working_directory_with_a_trailing_separator_is_normalised_first(self):
        """`working_directory` arrives as whatever the host wrote; unnormalised, `/work/` makes
        the base its own non-descendant and `.` resolves outside it."""
        assert confine_guest_path(".", "/work/") == "/work"
        assert confine_guest_path("a.txt", "/work/") == "/work/a.txt"

    def test_a_working_directory_with_a_dot_segment_is_normalised_first(self):
        assert confine_guest_path("a.txt", "/work/.") == "/work/a.txt"

    def test_a_backslash_is_refused_before_anything_is_joined(self):
        """The protocol has one path grammar and `\\` is not a separator in it, whatever the
        host OS — so this is refused rather than normalised into one."""
        with pytest.raises(ValueError) as caught:
            confine_guest_path("sub\\a.txt", _WORK_DIR)
        assert str(caught.value) == (
            r"path 'sub\\a.txt' contains a backslash, which is not a valid separator"
        )


class TestGuestPathRelativeTo:
    def test_the_base_itself_is_the_empty_string(self):
        """`""`, not `None` and not `"."`: the base is inside itself, and callers key on that
        empty string to mean the working directory's own entry."""
        assert guest_path_relative_to("/work", "/work") == ""

    def test_a_descendant_is_relative_to_the_base(self):
        assert guest_path_relative_to("/work/sub/a.txt", "/work") == "sub/a.txt"

    def test_a_sibling_sharing_a_string_prefix_is_not_inside(self):
        """`/work/sub2` starts with `/work/sub`, and a plain prefix test would call it a
        descendant."""
        assert guest_path_relative_to("/work/sub2", "/work/sub") is None

    def test_a_path_outside_the_base_is_none(self):
        assert guest_path_relative_to("/etc/passwd", "/work") is None

    def test_a_base_ending_in_a_separator_does_not_double_it(self):
        assert guest_path_relative_to("/a.txt", "/") == "a.txt"

    @pytest.mark.parametrize(
        ("path", "base"),
        [
            ("/work/../etc/passwd", "/work"),
            ("/work/sub/../../etc", "/work"),
            ("/work/./../etc", "/work"),
        ],
    )
    def test_a_traversal_that_leaves_the_base_is_outside_it(self, path: str, base: str):
        """Both operands are normalised, so a caller using this as its own containment check
        cannot be walked out of by a `..` that the string comparison would have carried."""
        assert guest_path_relative_to(path, base) is None

    def test_a_dot_segment_that_stays_inside_is_still_inside(self):
        assert guest_path_relative_to("/work/./sub/../a.txt", "/work") == "a.txt"


class TestGuestDirectoryChain:
    def test_the_chain_starts_above_the_working_directory(self):
        """A nested work dir has ancestors the guest can replace, so they are walked too."""
        assert guest_directory_chain("/a/b/work", "/a/b/work") == ("/a", "/a/b", "/a/b/work")

    def test_a_nested_guest_path_extends_the_chain(self):
        assert guest_directory_chain("/a/b/work/out/deeper", "/a/b/work") == (
            "/a",
            "/a/b",
            "/a/b/work",
            "/a/b/work/out",
            "/a/b/work/out/deeper",
        )

    def test_a_root_working_directory_has_no_ancestors_to_walk(self):
        assert guest_directory_chain("/out", "/") == ("/out",)

    def test_a_working_directory_with_a_trailing_separator_is_normalised_first(self):
        assert guest_directory_chain("/a/b/work/out", "/a/b/work/") == (
            "/a",
            "/a/b",
            "/a/b/work",
            "/a/b/work/out",
        )

    def test_a_working_directory_with_a_dot_segment_is_normalised_first(self):
        """Unnormalised, the `.` becomes a chain entry of its own and the guest path's own
        ancestors are dropped — the chain would stat everything except what it is for."""
        assert guest_directory_chain("/a/b/work/out", "/a/b/work/.") == (
            "/a",
            "/a/b",
            "/a/b/work",
            "/a/b/work/out",
        )


class TestRefuseSymlinkedParents:
    """The walk all three implementations of the pull surface now share.

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
        stat, statted = self._stat({"/work": EntryKind.DIRECTORY, "/work/out": EntryKind.DIRECTORY})
        asyncio.run(refuse_symlinked_parents(stat, "/work/out/a.png", _WORK_DIR))
        assert statted == ["/work", "/work/out"]

    def test_a_linked_parent_is_an_escape(self):
        stat, _ = self._stat({"/work": EntryKind.DIRECTORY, "/work/out": EntryKind.SYMLINK})
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(refuse_symlinked_parents(stat, "/work/out/a.png", _WORK_DIR))

    @pytest.mark.parametrize("kind", [EntryKind.FILE, EntryKind.OTHER])
    def test_any_other_non_directory_parent_is_enotdir(self, kind: EntryKind):
        stat, _ = self._stat({"/work": EntryKind.DIRECTORY, "/work/out": kind})
        with pytest.raises(NotADirectoryError):
            asyncio.run(refuse_symlinked_parents(stat, "/work/out/a.png", _WORK_DIR))

    def test_an_ancestor_above_the_working_directory_is_walked_too(self):
        """The `/acas -> /` case: a nested work dir has ancestors the guest can replace."""
        stat, _ = self._stat({"/acas": EntryKind.SYMLINK})
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(refuse_symlinked_parents(stat, "/acas/work/a.png", "/acas/work"))

    def test_a_missing_component_ends_the_walk_without_refusing(self):
        """A walk that finds nothing must not turn a missing output into a confinement failure."""
        stat, statted = self._stat({})
        asyncio.run(refuse_symlinked_parents(stat, "/work/out/a.png", _WORK_DIR))
        assert statted == ["/work"]

    def test_the_path_itself_is_not_walked_by_default(self):
        """Stat is `lstat`-like: the final component is described, not refused."""
        stat, _ = self._stat({"/work": EntryKind.DIRECTORY, "/work/link": EntryKind.SYMLINK})
        asyncio.run(refuse_symlinked_parents(stat, "/work/link", _WORK_DIR))

    def test_include_self_walks_it(self):
        """What `list_dir` needs — an enumeration passes through a link as a read does."""
        stat, _ = self._stat({"/work": EntryKind.DIRECTORY, "/work/link": EntryKind.SYMLINK})
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(refuse_symlinked_parents(stat, "/work/link", _WORK_DIR, include_self=True))
