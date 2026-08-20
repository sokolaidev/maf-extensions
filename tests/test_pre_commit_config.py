"""The pre-commit config's wiring: a hook whose stages or arguments are edited wrong must fail here.

The CI steps exercise the config through pre-commit, but they are themselves part of the
workflow file; this test pins the config's own contracts — which hook runs at which stage,
and that every non-default stage gets an end-to-end run somewhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

CONFIG = Path(__file__).parent.parent / ".pre-commit-config.yaml"
HOOKS = Path(__file__).parent.parent / ".githooks"


def _hooks() -> list[dict]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    # pre-commit's inheritance rule: a hook without `stages` takes the config's
    # `default_stages`, or every stage when the config does not set one.
    fallback = config.get("default_stages") or [
        "pre-commit",
        "pre-push",
        "commit-msg",
        "prepare-commit-msg",
        "manual",
    ]
    hooks: list[dict] = []
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            hook = dict(hook)
            hook["stages"] = hook.get("stages") or fallback
            hooks.append(hook)
    return hooks


def test_hooks_default_to_pre_commit_only() -> None:
    # A hook written without a `stages` key inherits every stage from the config default;
    # the default must keep the tiers where the tiers put it.
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config.get("default_stages") == ["pre-commit"]
    # A bare `pre-commit install` wires only these hook types; the documented one-liner must
    # install all three tiers, and CI's explicit stage runs would not catch a removal.
    assert config.get("default_install_hook_types") == ["pre-commit", "pre-push", "commit-msg"]


def test_every_installed_hook_type_has_a_tracked_script() -> None:
    # `core.hooksPath` runs what is tracked and nothing else, so a fourth tier added to the
    # config without a script here would never fire — no error, and no hook.
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    tracked = {path.name for path in HOOKS.iterdir() if path.is_file()}
    assert tracked == set(config["default_install_hook_types"])


def test_each_script_runs_its_own_tier() -> None:
    # The copy-paste failure: a script that names another tier's `--hook-type` runs the wrong
    # checks and passes, which is worse than not running.
    for path in sorted(HOOKS.iterdir()):
        assert f"--hook-type={path.name}" in path.read_text(encoding="utf-8"), path.name


def test_the_scripts_are_executable_in_the_index() -> None:
    # The index mode, not the filesystem's: a checkout on Windows reports 0644 either way, and
    # the bit that matters is the one a Linux contributor gets from the clone.
    listed = subprocess.run(
        ["git", "ls-files", "-s", "--", str(HOOKS)],
        capture_output=True,
        text=True,
        check=True,
        cwd=HOOKS.parent,
    ).stdout.splitlines()
    assert listed, "no tracked scripts found"
    assert all(line.startswith("100755 ") for line in listed), listed


def test_staged_hooks_match_their_contract() -> None:
    by_id = {hook["id"]: hook for hook in _hooks()}
    assert by_id["no-origin-identifiers"]["stages"] == ["pre-commit"]
    assert by_id["no-origin-identifiers-msg"]["stages"] == ["commit-msg"]
    assert by_id["no-commit-to-branch"]["stages"] == ["pre-commit", "manual"]
    # The pyright tier runs on push, whole tree: no file filter and unconditional, or a
    # deletion-only push skips it.
    for hook_id in ("pyright-packages", "pyroot"):
        assert by_id[hook_id]["stages"] == ["pre-push"]
        assert by_id[hook_id]["always_run"] is True
        assert by_id[hook_id]["pass_filenames"] is False
    assert by_id["no-origin-identifiers"]["args"] == ["--staged"]
    # `types: []` keeps staged symlinks in scope: pre-commit's default `types: [file]` would
    # filter them out, and a symlink's stored target is content the rules must see.
    assert by_id["no-origin-identifiers"]["types"] == []
    assert by_id["no-origin-identifiers-msg"]["args"] == ["--commit-msg"]
    # `_hooks()` models stages from this file alone: pre-commit merges the external repos'
    # manifests first, so a hook without a local `stages` key keeps its manifest's. Every
    # external hook must name its stages here, or a manifest-declared stage (as
    # check-added-large-files' pre-push) drifts out of this model silently.
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        if repo.get("repo") == "local":
            continue
        for hook in repo.get("hooks", []):
            assert isinstance(hook.get("stages"), list), f"{hook['id']}: stages not explicit"


def test_ci_exercises_every_non_default_stage() -> None:
    # Each stage a hook is declared for that is not the CI default must be run by a
    # `--hook-stage` step, or the step's wiring can rot undetected. Only the steps' `run`
    # values count — a comment mentioning the flag must not stand in for the command.
    workflow_path = Path(__file__).parent.parent / ".github" / "workflows" / "tests.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    run_values = " ".join(
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )
    declared = set()
    for hook in _hooks():
        declared.update(stage for stage in hook["stages"] if stage not in ("pre-commit", "manual"))
    for stage in declared:
        assert f"--hook-stage {stage}" in run_values, f"no CI step runs the {stage} stage"
