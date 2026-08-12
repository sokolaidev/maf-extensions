"""Tests for `maf_sandbox.paths` — the guest-path arithmetic kinds and backends share.

These three functions are the confinement check itself, so they are pinned directly here rather
than only through the fake that calls them: the cases that matter are the ones where a string
looks contained and is not.
"""

from __future__ import annotations

import pytest

from maf_sandbox.paths import confine_guest_path, guest_directory_chain, guest_path_relative_to

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
