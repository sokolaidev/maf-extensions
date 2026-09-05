"""What keeps `uv.lock`'s agent-framework current, held to naming one set of distributions.

`FRAMEWORK`, Dependabot's `allow` list and the requirements the workspace declares have to be
the same three lists, or the bot proposes what the drift run does not measure. Nothing here
reaches the network.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_DEPENDABOT = _ROOT / ".github" / "dependabot.yml"
_DRIFT_WORKFLOW = _ROOT / ".github" / "workflows" / "lock-drift.yml"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _script("check_locked_framework")
title = _script("check_title_diff")

#: A lockfile holds far more than the two entries under test, so the fixtures carry a third
#: package: a reader that matched on position or took the first entry would pass without it.
_LOCK = """\
version = 1

[[package]]
name = "agent-framework-core"
version = "{core}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "agent-framework-openai"
version = "{openai}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "ruff"
version = "0.15.22"
source = {{ registry = "https://pypi.org/simple" }}
"""

_LOCKED = _LOCK.format(core="1.13.0", openai="1.13.0")
_ADMITTED = _LOCK.format(core="1.17.0", openai="1.14.2")

#: A universal lock forks, and one distribution then holds a `[[package]]` record per branch.
#: The stale record comes **first** deliberately: a reader that keeps the last one it sees
#: reports the current version for the whole distribution and misses the branch that is behind.
_FORKED = """\
version = 1

[[package]]
name = "agent-framework-core"
version = "{first}"
source = {{ registry = "https://pypi.org/simple" }}
resolution-markers = ["python_full_version < '3.13'"]

[[package]]
name = "agent-framework-core"
version = "{second}"
source = {{ registry = "https://pypi.org/simple" }}
resolution-markers = ["python_full_version >= '3.13'"]

[[package]]
name = "agent-framework-openai"
version = "1.14.2"
source = {{ registry = "https://pypi.org/simple" }}
"""

_FORKED_ONE_BRANCH_BEHIND = _FORKED.format(first="1.13.0", second="1.17.0")
_FORKED_BOTH_CURRENT = _FORKED.format(first="1.17.0", second="1.17.0")

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def _declared_framework() -> set[str]:
    """Every agent-framework distribution the workspace asks for, wherever it asks."""
    declared: set[str] = set()
    sources = [_ROOT / "pyproject.toml", *sorted(_ROOT.glob("packages/*/pyproject.toml"))]
    for path in sources:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        requirements: list[str] = list(document.get("project", {}).get("dependencies", []))
        for group in document.get("dependency-groups", {}).values():
            requirements += [entry for entry in group if isinstance(entry, str)]
        for requirement in requirements:
            match = _REQUIREMENT_NAME.match(requirement)
            if match and match.group().startswith("agent-framework"):
                declared.add(match.group())
    return declared


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    # `PYTHONIOENCODING` because the report carries an em dash and a child process encodes its
    # streams in the host's locale: on Windows that is cp1252, which this cannot decode.
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_locked_framework.py"), *arguments],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=_ROOT,
    )


class TestTheListIsTheOneTheWorkspaceDeclares:
    """The two directions fail differently, so each is asserted on its own."""

    def test_every_declared_framework_distribution_is_watched(self):
        # A package adding a framework sibling would otherwise leave it on whatever the lock
        # happened to resolve, which is the gap this whole mechanism exists to close.
        assert _declared_framework() - set(check.FRAMEWORK) == set()

    def test_nothing_is_watched_that_nobody_declares(self):
        # The quieter direction: a distribution dropped from every pyproject stays in the list,
        # and `uv lock --upgrade-package` refuses a name it cannot resolve — so the drift run
        # reds on its own arguments rather than on the lockfile.
        assert set(check.FRAMEWORK) - _declared_framework() == set()


class TestReadingALockfile:
    def test_only_the_framework_entries_are_read(self):
        assert check.locked_versions(_LOCKED) == {
            "agent-framework-core": ("1.13.0",),
            "agent-framework-openai": ("1.13.0",),
        }

    def test_a_lock_already_current_has_no_drift(self):
        assert check.drift(_ADMITTED, _ADMITTED) == []

    def test_each_distribution_that_moved_is_named_with_both_versions(self):
        assert check.drift(_LOCKED, _ADMITTED) == [
            ("agent-framework-core", ("1.13.0",), ("1.17.0",)),
            ("agent-framework-openai", ("1.13.0",), ("1.14.2",)),
        ]

    def test_a_distribution_that_did_not_move_is_left_out(self):
        half = _LOCK.format(core="1.17.0", openai="1.13.0")
        assert check.drift(half, _ADMITTED) == [
            ("agent-framework-openai", ("1.13.0",), ("1.14.2",))
        ]

    def test_an_entry_the_committed_lock_never_had_is_reported_as_absent(self):
        # `was` is not a version here, so it must not be rendered as one: a maintainer reads
        # `None -> 1.17.0` as a downgrade the resolver made.
        without = "\n".join(
            line for line in _LOCKED.splitlines() if "agent-framework-openai" not in line
        )
        assert ("agent-framework-openai", (check.ABSENT,), ("1.14.2",)) in check.drift(
            without, _ADMITTED
        )


class TestALockThatForked:
    """A universal lock can record one distribution more than once, under different markers."""

    def test_every_record_is_read_rather_than_the_last_one(self):
        assert check.locked_versions(_FORKED_ONE_BRANCH_BEHIND)["agent-framework-core"] == (
            "1.13.0",
            "1.17.0",
        )

    def test_a_branch_left_behind_is_drift_even_when_the_other_is_current(self):
        # The regression this pins: reading one version per distribution keeps whichever record
        # came last, which here is the current one, and the stale branch is never reported.
        assert check.drift(_FORKED_ONE_BRANCH_BEHIND, _FORKED_BOTH_CURRENT) == [
            ("agent-framework-core", ("1.13.0", "1.17.0"), ("1.17.0", "1.17.0"))
        ]

    def test_a_fork_that_did_not_move_is_not_drift(self):
        assert check.drift(_FORKED_BOTH_CURRENT, _FORKED_BOTH_CURRENT) == []

    def test_the_table_names_every_version_on_each_side(self):
        rendered = check.report(check.drift(_FORKED_ONE_BRANCH_BEHIND, _FORKED_BOTH_CURRENT))
        assert "| `agent-framework-core` | 1.13.0, 1.17.0 | 1.17.0, 1.17.0 |" in rendered


class TestWhatARedRunSays:
    def test_a_current_lock_says_so_rather_than_printing_an_empty_table(self):
        assert "holds the newest" in check.report([])
        assert "|" not in check.report([])

    def test_the_table_carries_both_versions(self):
        rendered = check.report(check.drift(_LOCKED, _ADMITTED))
        assert "| `agent-framework-core` | 1.13.0 | 1.17.0 |" in rendered

    def test_the_command_upgrades_only_what_moved(self):
        half = _LOCK.format(core="1.17.0", openai="1.13.0")
        rendered = check.report(check.drift(half, _ADMITTED))
        assert "uv lock --upgrade-package agent-framework-openai\n" in rendered
        assert "agent-framework-core" not in rendered

    def test_the_summary_says_the_ranges_are_not_moving(self):
        """A reader who raises the floor instead costs the suite a release per package (#628)."""
        assert "not moving" in check.report(check.drift(_LOCKED, _ADMITTED))

    def test_the_annotation_is_one_line_and_names_the_drift(self):
        line = check.annotation(check.drift(_LOCKED, _ADMITTED))
        assert line.startswith("::error::")
        assert "\n" not in line
        assert "agent-framework-core 1.13.0 -> 1.17.0" in line


class TestTheCommandLine:
    def test_packages_prints_the_list_the_workflow_upgrades(self):
        finished = _run("--packages")
        assert finished.returncode == 0
        assert finished.stdout.split() == list(check.FRAMEWORK)

    def test_a_current_lock_passes(self, tmp_path: Path):
        current = tmp_path / "uv.lock"
        current.write_text(_ADMITTED, encoding="utf-8")
        assert _run(str(current), str(current)).returncode == 0

    def test_drift_fails_and_annotates(self, tmp_path: Path):
        committed = tmp_path / "committed.lock"
        resolved = tmp_path / "resolved.lock"
        committed.write_text(_LOCKED, encoding="utf-8")
        resolved.write_text(_ADMITTED, encoding="utf-8")
        finished = _run(str(committed), str(resolved))
        assert finished.returncode == 1
        assert "::error::" in finished.stderr
        assert "| `agent-framework-core` | 1.13.0 | 1.17.0 |" in finished.stdout

    @pytest.mark.parametrize("arguments", [(), ("one.lock",), ("one.lock", "two.lock", "three")])
    def test_a_call_it_cannot_answer_is_a_usage_error(self, arguments: tuple[str, ...]):
        assert _run(*arguments).returncode == 2


class TestDependabotProposesWhatTheDriftRunMeasures:
    """The bot and the check have to be about the same distributions, or one measures nothing."""

    @staticmethod
    def _update() -> dict:
        config = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
        assert config["version"] == 2
        updates = [entry for entry in config["updates"] if entry["package-ecosystem"] == "uv"]
        assert len(updates) == 1, f"expected one uv update, got {updates}"
        return updates[0]

    def test_the_lockfile_at_the_workspace_root_is_what_it_watches(self):
        assert self._update()["directory"] == "/"

    def test_it_may_propose_exactly_the_distributions_the_check_measures(self):
        # An allow list at all is what keeps the `ruff` and `pyright` bands the dev group pins
        # on purpose out of the bot's reach; this one being *these* names is what makes the
        # monthly drift run a measurement of the bot's work rather than of something else.
        allowed = {entry["dependency-name"] for entry in self._update()["allow"]}
        assert allowed == set(check.FRAMEWORK)

    def test_a_transitive_reading_of_the_framework_is_still_in_scope(self):
        # The default is direct dependencies only, and `agent-framework-core` is declared by
        # the workspace members rather than by the root manifest Dependabot reads here.
        types = {entry.get("dependency-type") for entry in self._update()["allow"]}
        assert types == {"all"}

    def test_its_titles_pass_the_pull_request_title_check(self):
        # Dependabot writes `<prefix>(deps): bump …`, and an unset prefix writes `Bump …`,
        # which is not a conventional commit and reds every proposal it opens.
        assert self._update()["commit-message"]["prefix"] in title._VALID_TYPES

    def test_its_titles_release_nothing(self):
        assert self._update()["commit-message"]["prefix"] in title._DOCUMENTATION_TYPES


class TestTheDriftRunAsksTheScript:
    """Two lists would drift, and the silent one upgrades nothing."""

    def test_the_upgrade_arguments_come_from_the_script(self):
        assert "check_locked_framework.py --packages" in _DRIFT_WORKFLOW.read_text("utf-8")

    def test_the_workflow_names_no_distribution_of_its_own(self):
        text = _DRIFT_WORKFLOW.read_text("utf-8")
        assert [name for name in check.FRAMEWORK if name in text] == []

    def test_it_runs_on_a_schedule_and_on_demand(self):
        # A schedule alone leaves no way to ask the question after fixing what it reported.
        workflow = yaml.safe_load(_DRIFT_WORKFLOW.read_text("utf-8"))
        # `on` is YAML 1.1's boolean, so safe_load gives the key back as `True`.
        assert "schedule" in workflow[True]
        assert "workflow_dispatch" in workflow[True]
