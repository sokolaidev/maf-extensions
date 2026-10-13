"""Image selection must not hide dependency changes behind release metadata."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from select_image_security import PROFILES, affected, changed_profiles, select  # noqa: E402

pytestmark = pytest.mark.workflow
PACKAGE = "packages/maf-sandbox"
METADATA = '[project]\nname = "maf-sandbox"\nversion = "0.1.0"\ndependencies = []\n'
LOCK = """version = 1
[[package]]
name = "maf-sandbox"
version = "0.1.0"
source = { editable = "packages/maf-sandbox" }
[[package]]
name = "dependency"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{url = "https://example.org/dependency.whl", hash = "sha256:original"}]
"""


def git(root, *args):
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, encoding="utf-8", stderr=subprocess.PIPE
    ).strip()


def commit(root, changes):
    for name, content in changes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "core.hooksPath=", "commit", "--allow-empty", "-qm", "test")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.org")
    git(tmp_path, "config", "commit.gpgsign", "false")
    base = commit(tmp_path, {f"{PACKAGE}/pyproject.toml": METADATA, "uv.lock": LOCK})
    return tmp_path, base


def release_changes():
    return {
        f"{PACKAGE}/pyproject.toml": METADATA.replace('version = "0.1.0"', 'version = "0.1.1"'),
        "uv.lock": LOCK.replace('version = "0.1.0"', 'version = "0.1.1"'),
        f"{PACKAGE}/CHANGELOG.md": "# Changelog\nNew release\n",
        ".release-please-manifest.json": '{"packages/maf-sandbox": "0.1.1"}',
    }


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_version_only_release_skips_builds_on_pr_and_merge(repository, event):
    root, base = repository
    head = commit(root, release_changes())
    profiles, reason = select(root, event, base, head)
    assert profiles == []
    assert "no image build, scan or security evidence" in reason


@pytest.mark.parametrize(
    "mutation",
    ["requirement", "third-party-version", "hash", "url", "source", "unpaired-version"],
)
def test_release_metadata_cannot_hide_dependency_or_lock_changes(repository, mutation):
    root, base = repository
    changes = release_changes()
    if mutation == "requirement":
        changes[f"{PACKAGE}/pyproject.toml"] = changes[f"{PACKAGE}/pyproject.toml"].replace(
            "dependencies = []", 'dependencies = ["dependency>=2"]'
        )
    else:
        old, new = {
            "third-party-version": ('version = "1.0.0"', 'version = "2.0.0"'),
            "hash": ("sha256:original", "sha256:changed"),
            "url": ("example.org", "another.example.org"),
            "source": ('editable = "packages/maf-sandbox"', 'virtual = "packages/maf-sandbox"'),
            "unpaired-version": ('version = "0.1.1"', 'version = "0.1.2"'),
        }[mutation]
        changes["uv.lock"] = changes["uv.lock"].replace(old, new)
    head = commit(root, changes)
    assert select(root, "pull_request", base, head)[0] == list(PROFILES)


def test_workspace_lock_version_without_matching_metadata_is_not_ignored(repository):
    root, base = repository
    head = commit(root, {"uv.lock": LOCK.replace('version = "0.1.0"', 'version = "0.1.1"')})
    assert select(root, "pull_request", base, head)[0] == list(PROFILES)


def test_release_with_image_change_selects_that_image(repository):
    root, base = repository
    head = commit(root, release_changes() | {"images/drawio-export/Dockerfile": "FROM scratch"})
    assert select(root, "pull_request", base, head)[0] == ["drawio-export"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("images/bicep-sandbox/Dockerfile", ["bicep", "bicep-avm", "sbx-bicep"]),
        ("images/sbx-template/Dockerfile", ["sbx-bicep"]),
        ("images/graphviz-sandbox/Dockerfile", ["graphviz"]),
        ("images/drawio-sandbox/render.py", ["drawio-sandbox"]),
        ("images/drawio-export/export.py", ["drawio-export"]),
        ("images/terraform-sandbox/install.py", list(PROFILES[6:11])),
        ("images/hyperlight-sandbox/Dockerfile", ["hyperlight"]),
        ("samples/experimental/hyperlight-aks/probe.py", ["hyperlight"]),
        ("packages/maf-sandbox/src/maf_sandbox/router.py", ["hyperlight"]),
        ("packages/maf-sandbox-codeact/src/maf_sandbox_codeact/tool.py", ["hyperlight"]),
        ("packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/worker.py", ["hyperlight"]),
        ("packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy/Dockerfile", ["egress-proxy"]),
        ("scripts/build_bicep_prepared_image.py", ["bicep-avm"]),
        ("scripts/bicep_avm_qualification.py", ["bicep-avm"]),
        ("scripts/bicep_qualification.py", ["bicep"]),
        ("images/bicep-sandbox/BICEP.md", []),
        ("scripts/terraform_avm_qualification.py", ["terraform-avm"]),
        ("scripts/build_scan_image.sh", list(PROFILES)),
        ("scripts/image_security_evidence.py", list(PROFILES)),
        ("scripts/select_image_security.py", list(PROFILES)),
        ("scripts/new_builder.py", list(PROFILES)),
        ("images/new-image/Dockerfile", list(PROFILES)),
        ("uv.lock", list(PROFILES)),
        ("pyproject.toml", list(PROFILES)),
        (".dockerignore", list(PROFILES)),
        (".github/workflows/image-security.yml", list(PROFILES)),
        ("packages/maf-sandbox-wslc/pyproject.toml", list(PROFILES)),
        ("packages/maf-sandbox/tests/test_router.py", []),
        ("packages/maf-sandbox/CHANGELOG.md", []),
        ("packages/maf-sandbox-wslc/src/maf_sandbox_wslc/backend.py", []),
        ("docs/security/container-images.md", []),
    ],
)
def test_build_input_mapping(path, expected):
    assert list(affected(path)) == expected


@pytest.fixture
def shared_inputs(repository):
    root, _ = repository
    builder = (ROOT / "scripts/build_scan_image.sh").read_text()
    catalogue = json.loads((ROOT / "images/release-profiles.json").read_text())
    before, count = re.subn(r"  terraform-avm\)\n.*?    ;;\n", "", builder, flags=re.DOTALL)
    assert count == 1
    old_catalogue = catalogue | {
        "profiles": [p for p in catalogue["profiles"] if p != "terraform-avm"]
    }
    base = commit(
        root,
        {
            "scripts/build_scan_image.sh": before,
            "images/release-profiles.json": json.dumps(old_catalogue),
        },
    )
    head = commit(
        root,
        {
            "scripts/build_scan_image.sh": builder,
            "images/release-profiles.json": json.dumps(catalogue),
        },
    )
    return root, base, head, builder, catalogue


@pytest.mark.parametrize("reverse", [False, True])
def test_unrelated_profile_membership_and_build_branch_are_isolated(shared_inputs, reverse):
    root, base, head, _, _ = shared_inputs
    if reverse:
        base, head = head, base
    assert select(root, "push", base, head)[0] == ["terraform-avm"]


def test_changed_command_selects_its_profile(shared_inputs):
    root, _, base, builder, _ = shared_inputs
    path = "scripts/build_scan_image.sh"
    head = commit(
        root, {path: builder.replace("--base-image maf-image-scan:base", "--base-image new-base")}
    )
    assert changed_profiles(root, base, head, path) == ("bicep-avm",)


@pytest.mark.parametrize(
    "mutation",
    [
        "prefix",
        "suffix",
        "fallthrough",
        "nested",
        "quote",
        "substitution",
        "duplicate",
        "missing",
        "heredoc",
        "commented-heredoc",
    ],
)
def test_unknown_or_shared_shell_changes_keep_full_coverage(shared_inputs, mutation):
    root, _, base, builder, _ = shared_inputs
    path = "scripts/build_scan_image.sh"
    changed = {
        "prefix": builder.replace("set -euo pipefail", "set -eu"),
        "suffix": builder + "echo shared\n",
        "fallthrough": builder.replace("    ;;", "    ;&", 1),
        "nested": builder.replace("  bicep)", '  bicep)\n    case "$2" in'),
        "quote": builder.replace("  bicep)\n", '  bicep)\n    docker "unterminated\n'),
        "substitution": builder.replace("  bicep)\n", "  bicep)\n    docker $(echo command)\n"),
        "duplicate": builder.replace("  bicep-avm)", "  bicep)"),
        "missing": None,
        "heredoc": builder.replace("\nPY\n", "\nMISSING\n"),
        "commented-heredoc": builder.replace(" <<'PY'", " # <<'PY'"),
    }[mutation]
    head = commit(root, {path: changed})
    assert changed_profiles(root, base, head, path) == PROFILES


def test_heredoc_contents_cannot_be_mistaken_for_dispatcher_branches(shared_inputs):
    root, _, base, builder, _ = shared_inputs
    path = "scripts/build_scan_image.sh"
    fake_branch = "  bicep)\n    docker changed\n    ;;\n"
    head = commit(
        root, {path: builder.replace("\nimport json\n", "\n" + fake_branch + "import json\n")}
    )
    assert changed_profiles(root, base, head, path) == ("drawio-export",)


def test_unchanged_unknown_shared_shell_cannot_narrow_later_changes(shared_inputs):
    root, _, _, builder, _ = shared_inputs
    path = "scripts/build_scan_image.sh"
    source = builder + 'source "$0"\n'
    base = commit(root, {path: source})
    head = commit(
        root, {path: source.replace("--base-image maf-image-scan:base", "--base-image newer")}
    )
    assert changed_profiles(root, base, head, path) == PROFILES


@pytest.mark.parametrize(
    "mutation", ["platform", "registry", "schema", "unknown", "duplicate", "invalid", "missing"]
)
def test_unknown_or_shared_catalogue_changes_keep_full_coverage(shared_inputs, mutation):
    root, _, base, _, catalogue = shared_inputs
    path = "images/release-profiles.json"
    updates = {
        "platform": {"platform": "linux/arm64"},
        "registry": {"registryPrefix": "ghcr.io/other"},
        "schema": {"schemaVersion": 2},
        "unknown": {"buildArgs": {}},
        "duplicate": {"profiles": catalogue["profiles"] * 2},
        "invalid": {"profiles": "bicep-avm"},
        "missing": {},
    }[mutation]
    head = commit(root, {path: None if mutation == "missing" else json.dumps(catalogue | updates)})
    assert changed_profiles(root, base, head, path) == PROFILES


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize(
    "path",
    [
        "images/bicep-sandbox/README.md",
        "images/bicep-sandbox/AVM.md",
        "images/sbx-template/README.md",
        "images/graphviz-sandbox/README.md",
        "images/drawio-export/README.md",
        "images/terraform-sandbox/README.md",
        "images/terraform-sandbox/AVM.md",
        "images/hyperlight-sandbox/README.md",
    ],
)
def test_consumer_guide_changes_do_not_select_image_builds(repository, event, path):
    root, base = repository
    head = commit(root, {path: "Updated consumer instructions"})
    assert select(root, event, base, head)[0] == []


def test_consumer_guide_does_not_hide_image_or_unknown_document_changes(repository):
    root, base = repository
    head = commit(
        root,
        {
            "images/graphviz-sandbox/README.md": "Updated instructions",
            "images/graphviz-sandbox/Dockerfile": "FROM new-base",
            "images/drawio-export/assets/help.md": "Bundled help",
        },
    )
    assert select(root, "push", base, head)[0] == ["graphviz", "drawio-export"]
    assert affected("images/new-image/README.md") == PROFILES
    assert affected("images/graphviz-sandbox/assets/README.md") == ("graphviz",)
    assert affected("packages/maf-sandbox/README.md") == ("hyperlight",)


def test_deleted_image_input_and_new_profile_both_select_scans(repository):
    root, _ = repository
    base = commit(root, {"images/drawio-export/old.py": "old"})
    head = commit(
        root, {"images/drawio-export/old.py": None, "images/graphviz-sandbox/new.py": "new"}
    )
    assert select(root, "push", base, head)[0] == ["graphviz", "drawio-export"]


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_scheduled_and_manual_runs_select_every_profile_without_a_diff(tmp_path, event):
    assert select(tmp_path, event, "", "")[0] == list(PROFILES)


@pytest.mark.parametrize("base", ["", "0" * 40, "a" * 40, "--all"])
def test_missing_or_invalid_comparison_selects_all_profiles(repository, base):
    root, head = repository
    assert select(root, "push", base, head)[0] == list(PROFILES)


def test_malformed_or_deleted_metadata_cannot_skip_scans(repository):
    root, base = repository
    for content in ("[project", None):
        head = commit(root, {f"{PACKAGE}/pyproject.toml": content})
        assert select(root, "pull_request", base, head)[0] == list(PROFILES)


def test_workflow_always_reports_selection_and_checks():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert "github.event_name" in workflow["concurrency"]["group"]
    jobs = workflow["jobs"]
    selector = jobs["select"]
    assert "if" not in selector
    assert selector["steps"][0]["with"]["fetch-depth"] == 0
    assert selector["steps"][-1]["env"]["BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    assert selector["steps"][-1]["run"] == "python3 scripts/select_image_security.py"
    assert jobs["scan"]["needs"] == "select"
    assert jobs["scan"]["if"] == "needs.select.outputs.scan == 'true'"
    assert jobs["check"]["needs"] == ["select", "scan"]
    assert jobs["check"]["if"] == "always()"
    assert "event=schedule" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="Production Bash check runs on Linux")
@pytest.mark.parametrize(
    "selection,required,result,success",
    [
        ("success", "true", "success", True),
        ("success", "false", "skipped", True),
        ("success", "true", "failure", False),
        ("success", "true", "cancelled", False),
        ("success", "true", "skipped", False),
        ("failure", "false", "skipped", False),
        ("cancelled", "", "skipped", False),
        ("success", "", "skipped", False),
    ],
)
def test_result_job_refuses_missing_or_failed_selected_scans(selection, required, result, success):
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    command = workflow["jobs"]["check"]["steps"][0]["run"]
    process = subprocess.run(
        ["bash", "-e", "-c", command],
        env=os.environ | {"SELECTION": selection, "SCAN_REQUIRED": required, "SCAN_RESULT": result},
        check=False,
    )
    assert (process.returncode == 0) is success


def test_selector_emits_matrix_and_scope_summary(tmp_path):
    output, summary = tmp_path / "output", tmp_path / "summary"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/select_image_security.py"), "--event", "schedule"],
        env=os.environ | {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)},
        check=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["profiles"]) == list(PROFILES)
    assert values["scan"] == "true"
    assert "Full coverage for schedule" in summary.read_text()
