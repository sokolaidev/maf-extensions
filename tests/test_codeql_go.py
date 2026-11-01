"""CodeQL must analyze owned Go sources in the patched proxy module."""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import codeql_go  # noqa: E402

pytestmark = pytest.mark.workflow


def test_source_inventory_covers_both_gateways_and_tools():
    sources = codeql_go.expected_sources(ROOT)
    assert len(sources) == 9
    assert {p.parts[1] for p in sources if p.parts[0] == "packages"} == {
        "maf-sandbox-docker",
        "maf-sandbox-wslc",
    }


def test_new_go_source_requires_an_explicit_build(monkeypatch):
    tracked = codeql_go.run("git", "ls-files", "--", "*.go", cwd=ROOT)
    monkeypatch.setattr(codeql_go, "run", lambda *args, **kwargs: tracked + "\nnew/source.go")
    with pytest.raises(ValueError, match="Go source inventory changed"):
        codeql_go.expected_sources(ROOT)


@pytest.mark.parametrize("context", codeql_go.CONTEXTS)
def test_build_identity_comes_from_the_production_dockerfile(context):
    revision, version = codeql_go.build_identity(ROOT / context)
    assert len(revision) == 40
    assert len(version.split(".")) == 3


@pytest.mark.parametrize(
    "dockerfile", ["FROM golang:latest", "FROM golang:1.26.9-alpine@sha256:abc"]
)
def test_missing_upstream_pin_is_rejected(tmp_path, dockerfile):
    (tmp_path / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    with pytest.raises(ValueError, match="Missing proxy build identity"):
        codeql_go.build_identity(tmp_path)


def test_manual_build_uses_patched_upstream_and_owned_sources(tmp_path, monkeypatch):
    for context in codeql_go.CONTEXTS:
        shutil.copytree(ROOT / context, tmp_path / context)
    sources = codeql_go.expected_sources(ROOT)
    monkeypatch.setattr(codeql_go, "expected_sources", lambda root: sources)
    calls = []
    revision, version = codeql_go.build_identity(ROOT / codeql_go.CONTEXTS[0])

    def fake_run(*args, cwd, env=None):
        calls.append((args, cwd, env))
        if args[:2] == ("go", "version"):
            return f"go version go{version} linux/amd64"
        if args[:2] == ("git", "init"):
            (Path(args[2]) / "internal/proxy").mkdir(parents=True)
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return revision
        return ""

    monkeypatch.setattr(codeql_go, "run", fake_run)
    output = tmp_path / "build"
    codeql_go.build(tmp_path, output)
    commands = [args for args, _, _ in calls]
    assert commands.count(("git", "fetch", "--depth=1", "origin", revision)) == 2
    assert sum(args[:3] == ("git", "apply", "--check") for args in commands) == 2
    assert commands.count(("go", "mod", "verify")) == 2
    assert sum(args[:3] == ("go", "build", "-a") for args in commands) == 6
    for context in codeql_go.CONTEXTS:
        source = tmp_path / context
        destination = output / context.parts[1]
        assert (destination / "go.mod").read_bytes() == (
            source / "dependencies/go.mod"
        ).read_bytes()
        assert (destination / "internal/proxy/credential_gate.go").read_bytes() == (
            source / "credential_gate.go"
        ).read_bytes()
        mapping = (output / "path-transformer.txt").read_text(encoding="utf-8")
        assert (
            f"#{source.as_posix()}\n{destination.as_posix()}/internal/proxy//credential_gate.go"
            in mapping
        )
    for args, _, env in calls:
        if args[:2] in (("go", "test"), ("go", "build")):
            assert env["GOFLAGS"] == "-mod=readonly"
            assert env["GOWORK"] == "off"
            assert env["CODEQL_PATH_TRANSFORMER"] == str(output / "path-transformer.txt")


@pytest.mark.parametrize("defect", ["missing", "wrong-bytes", "staged-path", "duplicate", None])
def test_coverage_requires_original_paths_and_bytes(tmp_path, monkeypatch, defect):
    source = Path("packages/proxy/credential_gate.go")
    original = tmp_path / source
    original.parent.mkdir(parents=True)
    original.write_bytes(b"package proxy\n")
    monkeypatch.setattr(codeql_go, "expected_sources", lambda root: [source])
    archive = tmp_path / "src.zip"
    name = original.as_posix().lstrip("/")
    with zipfile.ZipFile(archive, "w") as bundle:
        if defect != "missing":
            bundle.writestr(
                "staged/internal/proxy/credential_gate.go" if defect == "staged-path" else name,
                b"package wrong\n" if defect == "wrong-bytes" else original.read_bytes(),
            )
        if defect == "duplicate":
            bundle.writestr("duplicate/" + name, original.read_bytes())
    if defect is None:
        codeql_go.check_coverage(tmp_path, archive)
    else:
        with pytest.raises(ValueError, match="did not extract the original source"):
            codeql_go.check_coverage(tmp_path, archive)


def test_workflow_preserves_languages_and_checks_coverage_before_upload():
    workflow = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["analyze"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {row["language"] for row in matrix} == {
        "actions",
        "javascript-typescript",
        "python",
        "rust",
        "go",
    }
    assert next(row for row in matrix if row["language"] == "go")["build-mode"] == "manual"
    steps = job["steps"]
    analyze = next(step for step in steps if "codeql-action/analyze@" in step.get("uses", ""))
    upload = next(step for step in steps if "codeql-action/upload-sarif@" in step.get("uses", ""))
    coverage = next(step for step in steps if "codeql_go.py coverage" in step.get("run", ""))
    assert analyze["with"]["upload"] == "never"
    assert steps.index(analyze) < steps.index(coverage) < steps.index(upload)
    assert upload["if"] == "vars.CODEQL_ADVANCED_UPLOAD == 'true'"
    assert job["env"]["CODEQL_EXTRACTOR_GO_OPTION_EXTRACT_TESTS"] == "true"
    assert "pull_request_target" not in workflow.get("on", workflow.get(True, {}))
