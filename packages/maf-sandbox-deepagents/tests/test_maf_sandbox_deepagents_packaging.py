"""The publish-time experimental notice: emitted once, suppressible, `-W error`-safe.

Covers only the notice added to `maf_sandbox_deepagents/__init__.py` — behavior of the adapter
itself is `test_deepagents_sandbox.py`'s job.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings

import pytest

import maf_sandbox_deepagents


class TestExperimentalWarningCategory:
    """The category itself: a package-local `UserWarning` subclass, not shared."""

    def test_is_a_user_warning_not_a_future_or_deprecation_warning(self):
        category = maf_sandbox_deepagents.MafSandboxDeepagentsExperimentalWarning
        assert issubclass(category, UserWarning)
        assert not issubclass(category, DeprecationWarning)
        assert not issubclass(category, FutureWarning)


class TestExperimentalWarningEmission:
    """`importlib.reload` re-runs the module body, forcing a fresh emission to test against.

    The category class is redefined on every reload, so the match is on the stable
    `UserWarning` base plus the message text rather than on a class identity that a reload
    replaces.
    """

    def test_emitted_by_default_on_import(self):
        with pytest.warns(UserWarning, match=r"maf_sandbox_deepagents is experimental"):
            importlib.reload(maf_sandbox_deepagents)

    def test_suppressible_via_filterwarnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "ignore", category=maf_sandbox_deepagents.MafSandboxDeepagentsExperimentalWarning
            )
            warnings.warn(
                "maf_sandbox_deepagents is experimental and may change or be removed in future "
                "versions without notice.",
                category=maf_sandbox_deepagents.MafSandboxDeepagentsExperimentalWarning,
            )
        assert caught == []


class TestImportSurvivesDashWError:
    """The one hard requirement: `python -W error` must not turn this notice into a crash."""

    def test_import_exits_zero_under_dash_w_error(self):
        result = subprocess.run(
            [sys.executable, "-W", "error", "-c", "import maf_sandbox_deepagents"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
