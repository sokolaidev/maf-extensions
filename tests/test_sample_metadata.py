"""Every sample declares its own dependencies, and they have to match what it imports.

`agent.py` carries a PEP 723 `# /// script` block, which is what `uv run agent.py` reads. That
replaced a `pip install` line in each README — prose nothing could check, duplicated into
`verify-live.yml`, and free to drift from the imports it was supposed to describe.

Declaring them in the file does not make them correct; it makes them *checkable*. This is the
check: every `maf_sandbox*` module a sample imports must come from a distribution the block
names, and the block must parse as the TOML the spec says it is.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
from _version_prose import RELEASE_IN_PROSE, toml_comment

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())

#: PEP 723, as the spec writes it: a `script` block of `#`-prefixed lines.
_BLOCK = re.compile(
    r"(?m)^# /// script\s*$\s(?P<body>(?:^#(?:| .*)$\s)+)^# ///\s*$",
)


#: The module a distribution installs. Underscores in, hyphens out, and `maf_sandbox` itself
#: is the one that is not a suffix of anything.
def _distribution(module: str) -> str:
    return module.split(".")[0].replace("_", "-")


def _metadata(agent: Path) -> dict:
    match = _BLOCK.search(agent.read_text(encoding="utf-8"))
    assert match, (
        f"{agent.parent.name}/agent.py has no PEP 723 block. `uv run agent.py` — which is what "
        "its README says to run — has nothing to resolve without one."
    )
    body = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines(keepends=True)
    )
    return tomllib.loads(body)


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_the_block_parses_and_names_a_python_floor(sample: Path):
    metadata = _metadata(sample / "agent.py")
    assert metadata.get("requires-python"), "the block declares no requires-python"
    assert metadata.get("dependencies"), "the block declares no dependencies"


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_every_library_import_is_declared(sample: Path):
    """The check prose could not do.

    A sample that grows an import of another kind, and forgets the block, resolves fine in this
    workspace — every sibling is already on the path — and fails for the reader who follows the
    README. That is the exact failure the samples exist to not have.

    **Every** `.py` in the directory, not just `agent.py`. `uv run agent.py` builds one
    environment for whatever that run imports, so a module beside it — `diagram_kind.py`,
    `_scaffold.py` — is resolved from the same block and gets no say in it. Checking only the
    entry point would have gone blind the moment a sample grew a second file.
    """
    declared = {
        re.match(r"[A-Za-z0-9._-]+", dep).group(0)
        for dep in _metadata(sample / "agent.py")["dependencies"]
    }
    imported: dict[str, str] = {}
    for module in sorted(sample.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("maf_sandbox"):
                    imported.setdefault(_distribution(node.module), module.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("maf_sandbox"):
                        imported.setdefault(_distribution(alias.name), module.name)
    missing = sorted(set(imported) - declared)
    assert not missing, (
        f"{sample.name} imports {', '.join(f'{d} (in {imported[d]})' for d in missing)} and its "
        f"PEP 723 block does not declare it. Declared: {', '.join(sorted(declared))}."
    )


#: A released version, as release-please writes its changelog headings, newest first.
_CHANGELOG_HEADING = re.compile(r"(?m)^## \[(\d+(?:\.\d+)*)\]")


def _previous_release(changelog: str) -> tuple[int, int]:
    """The `(major, minor)` of the release before the newest one, read rather than computed.

    `(major, minor - 1)` is the obvious way to name the predecessor and it is wrong at a major
    boundary: at 1.0.0 it is `(1, -1)`, which every 0.x floor sorts below. The 1.0.0 Release PR
    would then fail with no legal move available — the samples cannot name 1.0.0 before it
    exists, and the pull request that would move them is not open yet. A changelog records what
    the predecessor actually was, and release-please writes it in the same commit that bumps
    the version, so the two cannot disagree.
    """
    headings = _CHANGELOG_HEADING.findall(changelog)
    assert len(headings) >= 2, "the changelog names fewer than two releases"
    major, minor = headings[1].split(".")[:2]
    return int(major), int(minor)


def _floors_outside_the_window(
    floors: set[tuple[int, int]], core: tuple[int, int], previous: tuple[int, int]
) -> list[tuple[int, int]]:
    """Declared floors this repository does not permit, lowest first.

    Exactly two are permitted — the current release and the one before it — because those are
    the only two states the release window can be in. Anything else is a floor that stopped
    moving.
    """
    return sorted(floor for floor in floors if floor not in (core, previous))


class TestTheWindowRule:
    """The two pure halves of the rule below, against versions this repository is not at yet.

    `TestTheDeclaredCoreFloor` reads the real tree, which is conformant by construction, so it
    cannot tell a one-release window from a three-release one — widening the tolerance left
    every test green when a reviewer tried it. These fix the width, and the major boundary the
    arithmetic this replaced got wrong.
    """

    def _changelog(self, *versions: str) -> str:
        return "# Changelog\n\n" + "".join(
            f"## [{version}](https://example.invalid/compare) (2026-01-01)\n\n### Features\n\n"
            for version in versions
        )

    def test_the_predecessor_is_the_second_heading(self):
        assert _previous_release(self._changelog("0.16.0", "0.15.0", "0.14.0")) == (0, 15)

    def test_a_patch_predecessor_keeps_its_minor(self):
        assert _previous_release(self._changelog("0.16.0", "0.15.1", "0.15.0")) == (0, 15)

    def test_across_a_major_the_predecessor_is_the_last_release_of_the_old_one(self):
        # The case the arithmetic could not express: `(1, 0 - 1)` is `(1, -1)`, and every 0.x
        # floor sorts below it, so the 1.0.0 Release PR failed with no legal floor to move to.
        assert _previous_release(self._changelog("1.0.0", "0.16.0")) == (0, 16)

    def test_a_floor_at_either_end_of_the_window_is_permitted(self):
        assert _floors_outside_the_window({(0, 16), (0, 15)}, (0, 16), (0, 15)) == []

    def test_two_releases_behind_is_not(self):
        # The width. A tolerance that quietly became three would let a floor rot inside it.
        assert _floors_outside_the_window({(0, 14)}, (0, 16), (0, 15)) == [(0, 14)]

    def test_a_floor_ahead_of_the_release_is_not(self):
        assert _floors_outside_the_window({(0, 17)}, (0, 16), (0, 15)) == [(0, 17)]

    def test_the_1_0_0_release_pull_request_stays_green(self):
        # End to end over the two halves: the state that used to be unrepresentable.
        changelog = self._changelog("1.0.0", "0.16.0")
        previous = _previous_release(changelog)
        assert _floors_outside_the_window({(0, 16)}, (1, 0), previous) == []


class TestTheDeclaredCoreFloor:
    """A floor is a claim, and until #343 nothing read it.

    `uv run --no-project` resolves the **newest** admissible version, so a floor that is too
    low is never exercised: the sample runs against current core, passes its live check, and
    the false claim sits in the file indefinitely. It bites only a consumer whose resolution
    is capped elsewhere — and it does not always bite loudly. Three samples declared `>=0.12`
    while depending on the `work_dir` default that moved in 0.13; resolved at their own floor,
    the source lands in `/work` while `bicepconfig.json` sits in `/maf-sandbox/work`, the
    compiler never finds it, and the model reports diagnostics produced without the configured
    rules. Nothing raises.

    Nothing short of *running* a sample at its declared floor can prove the floor honest, and
    that needs a live model and a real backend per version. So the claim is made true by
    construction instead: a sample is documentation of the current library, not a package with
    consumers to keep compatible, so every sample declares the current core minor and these
    tests are what stop that rotting. `scripts/set_dependents_range.py` moves them all after a
    core release, in the pull request that already moves the packages' range.
    """

    _PACKAGE = Path(__file__).resolve().parent.parent / "packages" / "maf-sandbox"

    def _core_minor(self) -> tuple[int, int]:
        text = (self._PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        major, minor = tomllib.loads(text)["project"]["version"].split(".")[:2]
        return int(major), int(minor)

    def _previous_minor(self) -> tuple[int, int]:
        return _previous_release((self._PACKAGE / "CHANGELOG.md").read_text(encoding="utf-8"))

    def _floor(self, sample: Path) -> tuple[int, ...] | None:
        found = [
            dep
            for dep in _metadata(sample / "agent.py")["dependencies"]
            if re.match(r"[A-Za-z0-9._-]+", dep.strip()).group(0) == "maf-sandbox"
        ]
        assert len(found) < 2, (
            f"{sample.name} names maf-sandbox {len(found)} times: {found}. Both this test and "
            "scripts/set_dependents_range.py read the first, so a second — a higher one in "
            "particular — would make the sample unresolvable with every check still green."
        )
        if not found:
            return None
        match = re.fullmatch(r"maf-sandbox>=(\d+(?:\.\d+)*)", found[0].strip())
        assert match, (
            f"{sample.name} declares {found[0]!r}. The floor has to be a bare "
            "`maf-sandbox>=X` — that is the shape scripts/set_dependents_range.py edits "
            "after a core release, and one it cannot read stops that step."
        )
        return tuple(int(part) for part in match.group(1).split("."))

    @pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
    def test_every_sample_declares_one(self, sample: Path):
        assert self._floor(sample) is not None, (
            f"{sample.name} names no maf-sandbox floor. Every sample imports the core, and "
            "taking whatever a backend distribution happens to pull in is how a sample comes "
            "to run on a version it was never checked against."
        )

    def test_they_all_declare_the_same_one(self):
        floors = {sample.name: self._floor(sample) for sample in _SAMPLE_DIRS}
        distinct = sorted({floor for floor in floors.values() if floor is not None})
        assert len(distinct) == 1, (
            "the samples declare different floors: "
            + ", ".join(f"{name}={floor}" for name, floor in sorted(floors.items()))
            + ". A per-sample floor is a per-sample claim, and each one is a claim nothing "
            "runs. They move together or they drift apart."
        )

    def test_it_is_the_current_core_minor(self):
        """The previous release is tolerated too, and only because of the release window.

        Not slack. The core's version in `pyproject.toml` moves when the Release PR merges;
        the wheel reaches PyPI later, because the publish run is held at "Approve and run".
        The range pull request that moves the samples is opened by that same merge — before
        the upload, which is why its own body says to check the version published before
        merging it. So between the Release PR merging and that one merging, the samples still
        name the release before this one, and they must: AGENTS.md forbids a floor naming a
        version that does not exist.

        One release is the whole window. RELEASING.md's order will not let a second core minor
        out until the dependents admitting this one have published, so a sample two releases
        behind means that pull request was closed or never merged, and the next Release PR
        going red is how you find out — early, and while it is still cheap to fix.
        """
        core, previous = self._core_minor(), self._previous_minor()
        floors = {floor[:2] for floor in map(self._floor, _SAMPLE_DIRS) if floor is not None}
        wrong = _floors_outside_the_window(floors, core, previous)
        ahead = [floor for floor in wrong if floor > core]
        assert not ahead, (
            f"a sample declares {ahead[0]} and this repository's core is at {core}. "
            "AGENTS.md: a dependency floor may only name a version that exists."
        )
        assert not wrong, (
            f"a sample declares {wrong[0]}; this repository's core is at {core} and the release "
            f"before it was {previous}. Move every sample's floor with "
            "`python scripts/set_dependents_range.py <released-version>`."
        )

    @pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
    def test_no_comment_beside_it_names_a_release(self, sample: Path):
        """The same trap `tests/test_release_config.py` closes for the packages.

        The bump script rewrites the constraint and never the prose beside it, so a sentence
        naming a version is stale one release later. Sample 09 carried "0.14 for
        Isolation.NONE" above its floor; nothing was going to notice when the floor moved on
        without it. Under a uniform floor there is nothing left for such a comment to justify.

        **Trailing comments count.** Checking only lines that *begin* a comment misses
        `"maf-sandbox-bicep",  # needs 0.14` — legal TOML, the most natural place to write the
        note, and on the very line the script rewrites. A reviewer put exactly that into a
        sample and every test stayed green.
        """
        match = _BLOCK.search((sample / "agent.py").read_text(encoding="utf-8"))
        assert match
        for line in match.group("body").splitlines():
            comment = toml_comment(line[1:])
            named = RELEASE_IN_PROSE.search(comment)
            assert not named, (
                f"{sample.name}: {comment.strip()!r} names {named.group(0)}. The constraint "
                "beside it is the source of truth and it moves without this comment — say "
                "what the dependency is for, not which release it points at."
            )


def test_no_readme_still_tells_a_reader_to_pip_install():
    """The block is the single source now; a README recipe beside it is a second one."""
    offenders = [
        path.relative_to(_SAMPLES).as_posix()
        for path in _SAMPLES.rglob("README.md")
        if "pip install" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"these still carry a pip recipe: {', '.join(offenders)}. The dependencies live in the "
        "`# /// script` block, and a second list is the drift this replaced."
    )


# ---------------------------------------------------------------------------
# Every floor names a version that has been released, not just the core one
# ---------------------------------------------------------------------------

#: A floor on a distribution this repository publishes: `maf-sandbox-docker>=0.4`.
_REPO_FLOOR = re.compile(r"^(maf-sandbox(?:-[a-z]+)?)\s*>=\s*(\d+(?:\.\d+)*)\s*$")

_PACKAGES = Path(__file__).resolve().parent.parent / "packages"


def _released(changelog: str) -> list[tuple[int, ...]]:
    """Every version this package's changelog records, newest first."""
    return [
        tuple(int(part) for part in heading.split("."))
        for heading in _CHANGELOG_HEADING.findall(changelog)
    ]


def _names_a_released_version(floor: tuple[int, ...], released: list[tuple[int, ...]]) -> bool:
    """Whether ``floor`` names a release that happened, at the precision it was written.

    `>=0.4` is a claim that `0.4` exists, and a `0.4.x` heading is what makes it true — so the
    comparison is on the components the floor actually wrote, not on padded tuples. Padding
    would read `>=0.4` as `0.4.0` and reject a package whose first `0.4` release was `0.4.1`,
    which release-please can produce when a patch and a feature land in one Release PR.
    """
    return any(version[: len(floor)] == floor for version in released)


def _repo_floors(sample: Path) -> list[tuple[str, tuple[int, ...]]]:
    """The `(distribution, floor)` pairs a sample declares on this repository's packages."""
    found: list[tuple[str, tuple[int, ...]]] = []
    for dependency in _metadata(sample / "agent.py")["dependencies"]:
        match = _REPO_FLOOR.match(dependency.strip())
        if match:
            found.append((match.group(1), tuple(int(part) for part in match.group(2).split("."))))
    return found


class TestAFloorNamesAVersionThatExists:
    """The pure half, against changelogs this repository does not have.

    Kept separate from the tree-reading test below for `TestTheWindowRule`'s reason: the real
    tree is conformant most of the time, so it cannot tell a working comparison from one that
    says yes to everything.
    """

    def _changelog(self, *versions: str) -> str:
        return "# Changelog\n\n" + "".join(
            f"## [{version}](https://example.invalid/compare) (2026-01-01)\n\n### Features\n\n"
            for version in versions
        )

    def test_a_released_minor_is_named_by_a_two_part_floor(self):
        assert _names_a_released_version((0, 4), _released(self._changelog("0.4.0", "0.3.3")))

    def test_an_unreleased_minor_is_not(self):
        # This repository's own state while a Release PR is open: the version is decided and
        # the changelog does not have it yet, because release-please writes both at merge.
        assert not _names_a_released_version((0, 4), _released(self._changelog("0.3.3", "0.3.2")))

    def test_a_minor_whose_first_release_was_a_patch_still_counts(self):
        """Why the comparison is on the floor's own width.

        Padding `(0, 4)` to `(0, 4, 0)` would reject this, and `0.4.0` never existing is a
        thing release-please does — a Release PR carrying a fix and a feature lands once.
        """
        assert _names_a_released_version((0, 4), _released(self._changelog("0.4.1", "0.3.3")))

    def test_a_three_part_floor_is_matched_exactly(self):
        released = _released(self._changelog("0.4.1", "0.4.0"))
        assert _names_a_released_version((0, 4, 1), released)
        assert not _names_a_released_version((0, 4, 2), released)

    def test_a_later_release_does_not_vouch_for_a_version_that_was_skipped(self):
        # `>=0.5` is unresolvable while only 0.4 and 0.6 exist, which is exactly the shape a
        # hand-written floor produces when someone guesses the next number and misses.
        assert not _names_a_released_version((0, 5), _released(self._changelog("0.6.0", "0.4.0")))


class TestEveryDeclaredFloorIsReleased:
    """AGENTS.md: *a dependency floor may only name a version that exists.*

    `TestTheDeclaredCoreFloor` above enforces that for `maf-sandbox` and nothing else, so a
    sample that declared `maf-sandbox-docker>=0.4` before that wheel shipped passed every check
    on a pull request and failed only in `verify-live` — which runs after a release, against
    PyPI, and is dispatched rather than gating the merge. Green pull request, red release.

    A changelog entry is the offline proxy for "published". It is not exact: release-please
    writes the entry when the Release PR merges, and the upload is held at "Approve and run"
    afterwards, so a version can be in a changelog for a while before it is on the index. That
    gap is minutes and always in the safe direction for the *next* branch to be written, while
    the gap this closes — a floor naming a version nobody has decided to release — is open
    indefinitely and invisible.
    """

    @pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
    def test_every_floor_it_declares(self, sample: Path):
        for distribution, floor in _repo_floors(sample):
            changelog = _PACKAGES / distribution / "CHANGELOG.md"
            assert changelog.is_file(), (
                f"{sample.name} declares a floor on {distribution}, which this repository does "
                f"not publish — there is no {changelog.relative_to(_PACKAGES.parent)}."
            )
            released = _released(changelog.read_text(encoding="utf-8"))
            printed = ".".join(str(part) for part in floor)
            assert _names_a_released_version(floor, released), (
                f"{sample.name} declares {distribution}>={printed} and {distribution} has not "
                f"released {printed} — its newest is "
                f"{'.'.join(str(part) for part in released[0])}. Samples resolve from PyPI, so "
                "this floor makes the sample unresolvable: `uv` refuses to install rather than "
                "the sample failing at import. Land the package half, release it, and raise the "
                "floor afterwards — AGENTS.md and RELEASING.md step 4."
            )
