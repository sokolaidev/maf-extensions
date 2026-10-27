"""Package reuse authenticates restored bytes and survives loss without weakening preparation."""

import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "images/terraform-sandbox"))
import prepare_providers as orchestration  # noqa: E402
from test_terraform_provider_source_build import built_package, provider  # noqa: E402

builder = orchestration.builder


@pytest.fixture
def package_build(monkeypatch):
    item = provider()
    data = built_package(item)
    calls = []

    def build(selected, work):
        calls.append(selected)
        return data, item["source_build"]["module_files"]

    monkeypatch.setattr(builder, "build", build)
    return item, data, calls


def test_cold_build_then_verified_hit_skips_compilation(tmp_path, package_build):
    item, data, calls = package_build
    first, cold = builder.cached_build(item, tmp_path)
    second, warm = builder.cached_build(item, tmp_path)
    assert first == second == data
    assert len(calls) == 1
    assert cold["cache"] == "miss" and warm["cache"] == "hit"
    assert warm["build_seconds"] == 0
    assert warm["verification_seconds"] >= 0
    assert warm["bytes"] == len(data)
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("value", [b"partial", b"", b"other package"])
def test_corrupt_cache_is_rebuilt_and_replaced(tmp_path, package_build, value):
    item, data, calls = package_build
    target = tmp_path / (builder.cache_key(item) + ".zip")
    target.write_bytes(value)
    actual, report = builder.cached_build(item, tmp_path)
    assert actual == target.read_bytes() == data
    assert report["cache"] == "rejected" and len(calls) == 1


def test_even_hash_matching_package_must_match_provenance(tmp_path, package_build):
    item, data, calls = package_build
    item["source_build"]["revision"] = "f" * 40
    target = tmp_path / (builder.cache_key(item) + ".zip")
    target.write_bytes(data)
    with pytest.raises(builder.prep.Refused, match="artifact-mismatch"):
        builder.cached_build(item, tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field", ["revision", "artifact", "go_version", "modules", "module_files", "sha256"]
)
def test_build_input_changes_invalidate_package_key(field):
    item = provider()
    changed = copy.deepcopy(item)
    changed["source_build"][field] = "changed"
    assert builder.cache_key(item) != builder.cache_key(changed)


@pytest.mark.parametrize("field", ["source", "version", "platform", "artifact"])
def test_provider_identity_changes_invalidate_package_key(field):
    item = provider()
    changed = copy.deepcopy(item)
    changed[field] = "changed"
    assert builder.cache_key(item) != builder.cache_key(changed)


def test_builder_and_toolchain_recipe_changes_invalidate_key(tmp_path, monkeypatch):
    paths = (
        "images/terraform-sandbox/build_providers.py",
        "images/terraform-sandbox/prepared.Dockerfile",
        "scripts/terraform_dependencies.py",
        "scripts/terraform_manifest.py",
        "images/terraform-sandbox/runner.py",
    )
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(builder, "__file__", str(tmp_path / paths[0]))
    item = provider()
    previous = builder.cache_key(item)
    for name in paths:
        with (tmp_path / name).open("a", encoding="utf-8") as file:
            file.write("\n# changed implementation\n")
        current = builder.cache_key(item)
        assert current != previous
        previous = current


def test_cache_unavailable_still_returns_verified_build(tmp_path, package_build):
    item, data, calls = package_build
    cache = tmp_path / "not-a-directory"
    cache.write_text("unavailable")
    actual, record = builder.cached_build(item, cache)
    assert actual == data and len(calls) == 1
    assert record["cache"] in {"miss", "rejected"} and "write_error" in record


def test_interrupted_write_preserves_previous_bytes_and_removes_partial(tmp_path, monkeypatch):
    target = tmp_path / "package.zip"
    target.write_bytes(b"previous")

    def fail(*args):
        raise OSError("interrupted before publication")

    monkeypatch.setattr(builder.os, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        builder.atomic_write(target, b"next")
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


def test_concurrent_writers_publish_one_complete_package(tmp_path, package_build):
    item, data, _ = package_build
    target = tmp_path / (builder.cache_key(item) + ".zip")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: builder.cached_build(item, tmp_path), range(12)))
    assert all(result[0] == data for result in results)
    assert target.read_bytes() == data
    assert list(tmp_path.iterdir()) == [target]


def test_unreviewed_fresh_build_never_enters_cache(tmp_path, package_build, monkeypatch):
    item, _, _ = package_build
    monkeypatch.setattr(builder, "build", lambda *a: (b"bad", item["source_build"]["module_files"]))
    with pytest.raises(builder.prep.Refused, match="artifact-digest"):
        builder.cached_build(item, tmp_path)
    assert not list(tmp_path.iterdir())


def test_cross_profile_snapshot_keeps_individual_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestration, "ROOT", tmp_path)
    item = provider()
    manifests = []
    for engine in ("terraform", "opentofu"):
        path = tmp_path / f"{engine}.json"
        path.write_text(
            json.dumps({"schema": 1, "engine": engine, "providers": [item], "modules": []})
        )
        manifests.append(path)
    assert orchestration.plan(manifests[0])["key"] == orchestration.plan(manifests[1])["key"]
    other = copy.deepcopy(item)
    other["version"] = "9.9.9"
    document = json.loads(manifests[1].read_text())
    document["providers"].append(other)
    manifests[1].write_text(json.dumps(document))
    assert orchestration.plan(manifests[0])["key"] != orchestration.plan(manifests[1])["key"]


def test_cli_reports_verified_reuse_and_refuses_cache_during_lock_updates(
    tmp_path, package_build, monkeypatch
):
    item, data, calls = package_build
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": 1, "engine": "terraform", "providers": [item], "modules": []})
    )
    output = tmp_path / "out"
    report = tmp_path / "report.jsonl"
    args = [
        "builder",
        "--manifest",
        str(manifest),
        "--output",
        str(output),
        "--cache-directory",
        str(tmp_path / "cache"),
        "--report",
        str(report),
    ]
    monkeypatch.setattr(sys, "argv", args)
    builder.main()
    builder.main()
    assert len(calls) == 1
    assert json.loads(report.read_text())["cache"] == "hit"
    assert builder.prep.built_provider(item, output) == data
    monkeypatch.setattr(sys, "argv", args + ["--update-lock", str(tmp_path / "proposed.json")])
    with pytest.raises(ValueError, match="without a package cache"):
        builder.main()


def test_timed_out_docker_build_removes_its_named_container(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestration, "ROOT", tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            raise orchestration.subprocess.TimeoutExpired(command, 7200)

    monkeypatch.setattr(orchestration.subprocess, "run", run)
    with pytest.raises(orchestration.subprocess.TimeoutExpired):
        orchestration.prepare(tmp_path / "manifest.json", tmp_path / "cache", tmp_path / "out")
    container = calls[1][calls[1].index("--name") + 1]
    assert calls[2] == ["docker", "rm", "--force", container]


@pytest.mark.workflow
def test_cache_workflow_preserves_qualification_and_uses_fresh_benchmark_runners():
    action = yaml.safe_load(
        (ROOT / ".github/actions/prepare-provider-cache/action.yml").read_text()
    )
    steps = action["runs"]["steps"]
    restore = next(s for s in steps if s.get("id") == "restore")
    assert restore["continue-on-error"] is True
    assert "inputs.namespace" in restore["with"]["restore-keys"]
    assert "steps.plan.outputs.key" in restore["with"]["key"]
    benchmark = yaml.safe_load((ROOT / ".github/workflows/provider-cache.yml").read_text())
    assert benchmark["jobs"]["warm"]["needs"] == "cold"
    for name in ("cold", "warm"):
        assert benchmark["jobs"][name]["runs-on"] == "ubuntu-latest"
    release = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    assert release["jobs"]["approve"]["environment"]["name"] == "container-release"
    providers = next(s for s in release["jobs"]["prepare"]["steps"] if s.get("id") == "providers")
    assert "github.run_attempt == 1" in providers["if"]
