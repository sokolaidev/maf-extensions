"""The shared reader behind both constraint-comment guards (#385).

Every case here is a *pair*: something that must be caught beside something that must not.
A one-sided test passes for a pattern that flags nothing and for one that flags everything,
and this file exists because both halves of that were shipped within a day of each other —
a guard that missed a trailing `0.13`, and a replacement that flagged the `1.0.0` the guard
is meant to permit.
"""

from __future__ import annotations

import pytest
from _version_prose import RELEASE_IN_PROSE, release_named_in, toml_comment


class TestWhereACommentCanHide:
    def test_a_leading_comment_is_read(self):
        assert toml_comment("    # 0.14 for Isolation.NONE").strip() == "# 0.14 for Isolation.NONE"

    def test_a_trailing_comment_is_read(self):
        # The blind spot in the packages' guard: it walked only upwards, so the one line the
        # bump script actually rewrites was the one line it never looked at.
        assert toml_comment('     "maf-sandbox-bicep",  # needs 0.14') == "# needs 0.14"

    def test_a_line_with_no_comment_reads_empty(self):
        assert toml_comment('     "maf-sandbox>=0.16",') == ""

    def test_a_hash_inside_a_quoted_value_is_not_a_comment(self):
        assert toml_comment('     "pkg @ https://host/w.whl#sha256=0.14",') == ""


class TestWhatCountsAsNamingARelease:
    @pytest.mark.parametrize("prose", ["# 0.13 for the work_dir default", "# 0.13.0 for it"])
    def test_a_pre_1_0_release_is_named(self, prose: str):
        assert RELEASE_IN_PROSE.search(prose)

    def test_the_stability_boundary_is_not(self):
        """`1.0.0` is the one version these comments are allowed to mention.

        It says "this project is pre-1.0 and may break", which stays true release after
        release — the opposite of a pointer that goes stale. Every package's dependency
        comment carries some form of it, so a pattern matching the `0.0` inside it fails all
        five for saying the thing the guard exists to permit.
        """
        assert not RELEASE_IN_PROSE.search("every release before 1.0.0 may include breaking")

    def test_a_python_version_is_not(self):
        assert not RELEASE_IN_PROSE.search("# needs python 3.12 or newer")

    def test_the_two_halves_compose(self):
        assert release_named_in('  "maf-sandbox>=0.16",  # 0.13 for the default') == "0.13"
        assert release_named_in('  "maf-sandbox>=0.16",  # safe before 1.0.0') is None
