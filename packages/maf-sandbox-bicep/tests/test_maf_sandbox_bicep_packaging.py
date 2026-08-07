"""The publish-time experimental notice: emitted once, suppressible, `-W error`-safe.

New file (not the existing `test_bicep_workload.py`) so this doesn't collide with that
file's concurrent edits elsewhere in the same release. Covers only the notice added to
`maf_sandbox_bicep/__init__.py` — behavior of the tool itself is that file's job.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings

import pytest

import maf_sandbox_bicep


class TestExperimentalWarningCategory:
    """The category itself: a package-local `UserWarning` subclass, not shared."""

    def test_is_a_user_warning_not_a_future_or_deprecation_warning(self):
        assert issubclass(maf_sandbox_bicep.MafSandboxBicepExperimentalWarning, UserWarning)
        assert not issubclass(
            maf_sandbox_bicep.MafSandboxBicepExperimentalWarning, DeprecationWarning
        )
        assert not issubclass(maf_sandbox_bicep.MafSandboxBicepExperimentalWarning, FutureWarning)


class TestExperimentalWarningEmission:
    """`importlib.reload` re-runs the module body, forcing a fresh emission to test against.

    The category class is also redefined on every reload — a `class` statement always
    builds a fresh type object — so a
    `maf_sandbox_bicep.MafSandboxBicepExperimentalWarning` reference captured *before*
    `pytest.warns(...)` enters its block would already be stale by the time the reloaded
    module's `class` statement replaces the module attribute and the new class is what
    `warnings.warn` actually raises. Matching on the stable `UserWarning` base plus the
    message text sidesteps that identity trap.
    """

    def test_emitted_by_default_on_import(self):
        with pytest.warns(UserWarning, match=r"maf_sandbox_bicep is experimental"):
            importlib.reload(maf_sandbox_bicep)

    def test_suppressible_via_filterwarnings(self):
        """Exercises `filterwarnings(category=...)` directly, not through another reload.

        `filterwarnings`'s category matching is identity-based too, so — for the same
        reason `test_emitted_by_default_on_import` avoids it above — this uses the class
        object this test file already imported rather than one produced by a fresh reload.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "ignore", category=maf_sandbox_bicep.MafSandboxBicepExperimentalWarning
            )
            warnings.warn(
                "maf_sandbox_bicep is experimental and may change or be removed in future "
                "versions without notice.",
                category=maf_sandbox_bicep.MafSandboxBicepExperimentalWarning,
            )
        assert caught == []


class TestImportSurvivesDashWError:
    """The one hard requirement: `python -W error` must not turn this notice into a crash."""

    def test_import_exits_zero_under_dash_w_error(self):
        result = subprocess.run(
            [sys.executable, "-W", "error", "-c", "import maf_sandbox_bicep"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
