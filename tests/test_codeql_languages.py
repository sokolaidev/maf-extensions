"""PR CodeQL selection must cover changed sources without scanning unrelated languages."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import codeql_languages as selector  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/maintainers.md", []),
        (".release-please-manifest.json", []),
        ("packages/maf-sandbox-document/CHANGELOG.md", []),
        ("packages/maf-sandbox-document/pyproject.toml", ["python"]),
        ("uv.lock", ["python"]),
        ("images/document-export/requirements.in", ["python"]),
        ("images/document-export/requirements.txt", ["python"]),
        ("samples/example.py", ["python"]),
        ("package/module.pyi", ["python"]),
        ("docs/security/status/status.js", ["javascript-typescript"]),
        ("ui/component.tsx", ["javascript-typescript"]),
        ("ui/package-lock.json", ["javascript-typescript"]),
        ("ui/tsconfig.build.json", ["javascript-typescript"]),
        ("ui/pnpm-lock.yaml", ["javascript-typescript"]),
        ("scripts/experiments/mxc_native_state/src/main.rs", ["rust"]),
        ("scripts/experiments/mxc_session_patch/Cargo.toml.template", ["rust"]),
        ("scripts/experiments/mxc_native_state/Cargo.lock", ["rust"]),
        ("scripts/experiments/mxc_session_patch/storage-host.patch", ["rust"]),
        ("rust-toolchain.toml", ["rust"]),
        ("nested/.cargo/config.toml", ["rust"]),
        ("scripts/proxy_dependency_imports.go", ["go"]),
        ("tests/fixtures/openclaw_docker_authz/main_test.go", ["go"]),
        ("module/go.mod", ["go"]),
        ("module/go.sum", ["go"]),
        ("go.work", ["go"]),
        ("scripts/codeql_go.py", ["python", "go"]),
        ("tests/test_codeql_go.py", ["python", "go"]),
        (".github/workflows/tests.yml", ["actions"]),
        (".github/actions/build/action.yaml", ["actions"]),
        (".github/workflows/codeql.yml", list(selector.LANGUAGES)),
        (".github/codeql/config.yml", list(selector.LANGUAGES)),
        ("scripts/codeql_languages.py", list(selector.LANGUAGES)),
        ("tests/test_codeql_languages.py", list(selector.LANGUAGES)),
    ],
)
def test_relevant_inputs_select_their_languages(path, expected):
    assert selector.select_languages([path]) == expected


@pytest.mark.parametrize("context", selector.CONTEXTS)
@pytest.mark.parametrize("name", ["Dockerfile", "iron.patch", "dependencies/go.sum"])
def test_proxy_build_inputs_select_go(context, name):
    assert selector.select_languages([(context / name).as_posix()]) == ["go"]


def test_document_release_selects_only_python():
    paths = [
        ".release-please-manifest.json",
        "packages/maf-sandbox-document/CHANGELOG.md",
        "packages/maf-sandbox-document/pyproject.toml",
        "uv.lock",
    ]
    assert selector.select_languages(paths) == ["python"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_events_scan_every_language_without_a_diff(tmp_path, event):
    matrix = selector.analysis_matrix(event, tmp_path)
    assert [row["language"] for row in matrix] == list(selector.LANGUAGES)
    assert {row["language"] for row in matrix if row["build-mode"] == "manual"} == {"go"}


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("config", "user.name", "CodeQL test")
    git("config", "user.email", "codeql@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (tmp_path / "old.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "removed.rs").write_text("fn main() {}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    return tmp_path, git, base


def test_diff_covers_renames_deletions_and_files_beyond_trigger_limits(repository):
    root, git, base = repository
    (root / "old.go").rename(root / "renamed.txt")
    (root / "removed.rs").unlink()
    for number in range(305):
        (root / f"doc-{number}.md").write_text("documentation\n", encoding="utf-8")
    (root / "z space.py").write_text("print('hello')\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "changes")
    head = git("rev-parse", "HEAD")
    paths = selector.changed_paths(root, base, head)
    assert len(paths) == 309
    assert {"old.go", "renamed.txt", "removed.rs", "z space.py"} <= set(paths)
    assert [
        row["language"] for row in selector.analysis_matrix("pull_request", root, base, head)
    ] == ["python", "rust", "go"]


def test_diff_ignores_changes_only_on_the_base_branch(repository):
    root, git, base = repository
    (root / "readme.md").write_text("docs\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "PR docs")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", base)
    (root / "base-only.js").write_text("console.log('main');\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "main advances")
    assert selector.analysis_matrix("pull_request", root, git("rev-parse", "HEAD"), head) == []


@pytest.mark.parametrize("base", ["", "--help", "main"])
def test_invalid_pr_identity_fails_instead_of_skipping(tmp_path, base):
    with pytest.raises(ValueError, match="full base and head"):
        selector.analysis_matrix("pull_request", tmp_path, base, "a" * 40)


def test_missing_commit_fails_instead_of_skipping(repository):
    root, _, base = repository
    with pytest.raises(subprocess.CalledProcessError):
        selector.analysis_matrix("pull_request", root, base, "f" * 40)


@pytest.mark.parametrize("paths", [[], ["source.py"], ["source.go", "other.rs"]])
def test_workflow_outputs_matrix_and_summary(tmp_path, monkeypatch, paths):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setattr(selector, "changed_paths", lambda *args: paths)
    selector.main()
    output = (tmp_path / "output").read_text(encoding="utf-8")
    assert output.startswith("matrix=")
    matrix = json.loads(output.removeprefix("matrix="))
    assert [row["language"] for row in matrix] == selector.select_languages(paths)
    assert bool(matrix) == bool(paths)
    assert (tmp_path / "summary").read_text(encoding="utf-8").startswith("CodeQL languages:")


def test_workflow_skips_empty_matrix_and_reports_selection_failures():
    workflow = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    jobs = workflow["jobs"]
    select = jobs["select"]
    assert select["outputs"]["matrix"] == "${{ steps.select.outputs.matrix }}"
    checkout, step = select["steps"]
    assert checkout["with"]["fetch-depth"] == 0
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert step["id"] == "select"
    assert step["run"] == "python3 scripts/codeql_languages.py"
    assert jobs["analyze"]["needs"] == "select"
    assert jobs["analyze"]["if"] == "needs.select.outputs.matrix != '[]'"
    assert (
        jobs["analyze"]["strategy"]["matrix"]["include"]
        == "${{ fromJSON(needs.select.outputs.matrix) }}"
    )
    assert jobs["report-failure"]["needs"] == ["select", "analyze"]
    assert jobs["report-failure"]["if"] == (
        "always() && (needs.select.result == 'failure' || needs.analyze.result == 'failure')"
    )
