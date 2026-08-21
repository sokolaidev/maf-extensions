"""The pre-commit config's wiring: a hook whose stages or arguments are edited wrong must fail here.

The CI steps exercise the config through pre-commit, but they are themselves part of the
workflow file; this test pins the config's own contracts — which hook runs at which stage,
and that every non-default stage gets an end-to-end run somewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / ".pre-commit-config.yaml"
INSTALLER = ROOT / "scripts" / "install_hooks.py"


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


def test_installer_covers_every_installed_hook_type() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push", "commit-msg"}


def test_installer_writes_runtime_resolving_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    shutil.copy2(CONFIG, repo / CONFIG.name)

    subprocess.run([sys.executable, str(INSTALLER)], cwd=repo, check=True)

    hook_dir = (
        repo
        / Path(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    ).resolve()
    assert hook_dir == (repo / ".git" / "hooks").resolve()
    assert (
        subprocess.run(
            ["git", "config", "--get", "core.hooksPath"], cwd=repo, capture_output=True
        ).returncode
        != 0
    )
    unrelated = hook_dir / "post-commit"
    unrelated.write_text("user hook\n", encoding="utf-8")
    subprocess.run([sys.executable, str(INSTALLER)], cwd=repo, check=True)
    assert unrelated.read_text(encoding="utf-8") == "user hook\n"
    for hook_type in ("pre-commit", "pre-push", "commit-msg"):
        hook = hook_dir / hook_type
        assert hook.is_file()
        assert f"--hook-type={hook_type}" in hook.read_text(encoding="utf-8")
        if os.name != "nt":
            assert hook.stat().st_mode & 0o111


def test_installer_refuses_symlinked_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    shutil.copy2(CONFIG, repo / CONFIG.name)
    hook_dir = repo / ".git" / "hooks"
    hook_dir.mkdir(exist_ok=True)
    try:
        (hook_dir / "pre-commit").symlink_to(tmp_path / "missing-hook")
    except OSError:
        return

    result = subprocess.run(
        [sys.executable, str(INSTALLER)], cwd=repo, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "Refusing to replace" in result.stderr


def test_installer_refuses_an_existing_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    shutil.copy2(CONFIG, repo / CONFIG.name)
    subprocess.run(["git", "config", "core.hooksPath", "custom-hooks"], cwd=repo, check=True)

    result = subprocess.run(
        [sys.executable, str(INSTALLER)], cwd=repo, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "unset it before installing" in result.stderr


def test_installer_survives_branch_without_tracked_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    shutil.copy2(CONFIG, repo / CONFIG.name)
    (repo / ".githooks").mkdir()
    (repo / ".githooks" / "pre-commit").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "hooks"], cwd=repo, check=True)
    subprocess.run([sys.executable, str(INSTALLER)], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "--quiet", "-b", "old"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "--quiet", ".githooks/pre-commit"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "old branch"],
        cwd=repo,
        env=os.environ | {"SKIP": "no-origin-identifiers-msg"},
        check=True,
    )

    configured = (
        repo
        / Path(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    ).resolve()
    assert configured.is_dir()
    assert (configured / "pre-commit").is_file()
    assert (
        subprocess.run(
            ["git", "config", "--get", "core.hooksPath"], cwd=repo, capture_output=True
        ).returncode
        != 0
    )

    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "--quiet", str(linked), "HEAD"], cwd=repo, check=True)
    linked_configured = (
        linked
        / Path(
            subprocess.run(
                ["git", "-C", str(linked), "rev-parse", "--git-path", "hooks"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    ).resolve()
    assert linked_configured == configured


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
