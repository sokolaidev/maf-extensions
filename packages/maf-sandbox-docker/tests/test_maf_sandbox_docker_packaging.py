"""The publish-time experimental notice: emitted once, suppressible, `-W error`-safe.

Covers only the notice added to `maf_sandbox_docker/__init__.py` — behavior of the backend
itself is `test_docker_backend.py`'s job.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings

import pytest

import maf_sandbox_docker


class TestExperimentalWarningCategory:
    """The category itself: a package-local `UserWarning` subclass, not shared."""

    def test_is_a_user_warning_not_a_future_or_deprecation_warning(self):
        assert issubclass(maf_sandbox_docker.MafSandboxDockerExperimentalWarning, UserWarning)
        assert not issubclass(
            maf_sandbox_docker.MafSandboxDockerExperimentalWarning, DeprecationWarning
        )
        assert not issubclass(maf_sandbox_docker.MafSandboxDockerExperimentalWarning, FutureWarning)


class TestExperimentalWarningEmission:
    """`importlib.reload` re-runs the module body, forcing a fresh emission to test against.

    The category class is redefined on every reload — a `class` statement always builds a fresh
    type object — so a reference captured before `pytest.warns(...)` enters its block would be
    stale by the time the reloaded module's `class` statement replaces the module attribute.
    Matching on the stable `UserWarning` base plus the message text sidesteps that identity trap.
    """

    def test_emitted_by_default_on_import(self):
        with pytest.warns(UserWarning, match=r"maf_sandbox_docker is experimental"):
            importlib.reload(maf_sandbox_docker)

    def test_suppressible_via_filterwarnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "ignore", category=maf_sandbox_docker.MafSandboxDockerExperimentalWarning
            )
            warnings.warn(
                "maf_sandbox_docker is experimental and may change or be removed in future "
                "versions without notice.",
                category=maf_sandbox_docker.MafSandboxDockerExperimentalWarning,
            )
        assert caught == []


class TestImportSurvivesDashWError:
    """The one hard requirement: `python -W error` must not turn this notice into a crash."""

    def test_import_exits_zero_under_dash_w_error(self):
        result = subprocess.run(
            [sys.executable, "-W", "error", "-c", "import maf_sandbox_docker"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
