"""Source-built providers refuse unreviewed packages and preserve distinct lock identities."""

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "images/terraform-sandbox"))
sys.path.insert(0, str(ROOT / "scripts"))
import build_providers as builder  # noqa: E402
from terraform_manifest import retain_source_builds  # noqa: E402

prep = builder.prep


def provider():
    return copy.deepcopy(
        json.loads(
            (ROOT / "images/terraform-sandbox/dependencies.terraform-avm.json").read_text(
                encoding="utf-8"
            )
        )["providers"][0]
    )


def built_package(item):
    files = {
        "terraform-provider-test_v1.0.0": b"rebuilt",
        "LICENSE": b"license",
        "go.mod": b"module test",
        "go.sum": b"sum",
    }
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    plan = item["source_build"]
    plan["module_files"] = {name: hashes[name] for name in ("go.mod", "go.sum")}
    record = {
        "builder": "SOKOLAI",
        "source": item["source"],
        "version": item["version"],
        "upstream_artifact": item["artifact"],
        "inputs": {
            key: value for key, value in plan.items() if key not in {"sha256", "module_files"}
        },
        "files": hashes,
    }
    files["build.json"] = json.dumps(record).encode()
    data = builder.package(files)
    plan["sha256"] = hashlib.sha256(data).hexdigest()
    return data


@pytest.mark.parametrize(
    "change", ["repository", "revision", "archive", "toolchain", "module", "hash", "module-hash"]
)
def test_invalid_source_build_policy_is_refused(change):
    plan = provider()["source_build"]
    if change == "repository":
        plan["repository"] = "https://unapproved.example/repo"
    elif change == "revision":
        plan["revision"] = "main"
    elif change == "archive":
        plan["artifact"]["url"] = plan["artifact"]["url"].replace(plan["revision"], "main")
    elif change == "toolchain":
        plan["go_version"] = "auto"
    elif change == "module":
        plan["modules"]["golang.org/x/net"] = "latest"
    elif change == "hash":
        plan["sha256"] = "missing"
    else:
        plan["module_files"]["go.sum"] = "missing"
    with pytest.raises(prep.Refused):
        prep.provider_build_policy(plan)


def test_source_build_never_falls_back_to_upstream(tmp_path, monkeypatch):
    item = provider()
    monkeypatch.setattr(prep, "fetch", lambda *a, **kw: pytest.fail("upstream fallback"))
    with pytest.raises(prep.Refused, match="artifact-mismatch"):
        prep.built_provider(item, None)
    with pytest.raises(FileNotFoundError):
        prep.built_provider(item, tmp_path)


def test_modified_provider_package_is_refused(tmp_path):
    item = provider()
    path = tmp_path / item["source"] / item["version"] / "linux_amd64.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unexpected bytes")
    with pytest.raises(prep.Refused, match="artifact-digest"):
        prep.built_provider(item, tmp_path)


def test_preparation_records_the_built_identity_and_preserves_upstream_evidence(tmp_path):
    item = provider()
    data = built_package(item)
    item["source_build"]["sha256"] = hashlib.sha256(data).hexdigest()
    builds = tmp_path / "builds"
    path = builds / item["source"] / item["version"] / "linux_amd64.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    output = tmp_path / "prepared"
    prep.prepare(
        {"schema": 1, "engine": "terraform", "providers": [item], "modules": []},
        output,
        provider_builds=builds,
    )
    record = json.loads((output / "receipt.json").read_text())["providers"][0]
    assert record["sha256"] == item["source_build"]["sha256"] != item["artifact"]["sha256"]
    assert record["h1"] == prep.package_hash(data)
    assert record["upstream_artifact"] == item["artifact"]
    assert record["source_build"] == item["source_build"]
    assert record["provenance"].startswith("SOKOLAI source build")


def test_package_is_deterministic_and_changes_lock_hash_on_binary_change():
    first = {"LICENSE": b"notice", "terraform-provider-test_v1.0.0": b"one"}
    a = builder.package(first)
    assert a == builder.package(dict(reversed(list(first.items()))))
    first["terraform-provider-test_v1.0.0"] = b"two"
    assert prep.package_hash(a) != prep.package_hash(builder.package(first))
    with zipfile.ZipFile(io.BytesIO(a)) as archive:
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


@pytest.mark.parametrize("mismatch", ["toolchain", "module", "replacement"])
def test_build_refuses_security_input_drift_before_compilation(tmp_path, monkeypatch, mismatch):
    item = provider()
    plan = item["source_build"]

    def output(command, **kwargs):
        if command == ["go", "env", "GOVERSION"]:
            return "go1.26.0" if mismatch == "toolchain" else plan["go_version"]
        selected = {"Version": plan["modules"][command[-1]]}
        if mismatch == "module":
            selected["Version"] = "v0.1.0"
        else:
            selected["Replace"] = {"Dir": "/other"}
        return json.dumps(selected)

    def run(command, **kwargs):
        assert command[:2] != ["go", "build"]

    monkeypatch.setattr(builder.subprocess, "check_output", output)
    monkeypatch.setattr(builder.subprocess, "run", run)
    monkeypatch.setattr(prep, "fetch", lambda *a, **kw: b"source")
    monkeypatch.setattr(builder, "unpack", lambda *a: None)
    with pytest.raises(ValueError, match="mismatch"):
        builder.build(item, tmp_path)


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "nested/../outside", "nested\\outside"]
)
def test_source_unpack_refuses_escape_paths(tmp_path, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("source/", "")
        info = zipfile.ZipInfo("source/placeholder")
        info.filename = "source/" + name
        archive.writestr(info, "payload")
    with pytest.raises(ValueError, match="escapes"):
        builder.unpack(data.getvalue(), tmp_path)


def test_source_unpack_allows_upstream_test_names_with_spaces(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("source/", "")
        archive.writestr("source/tests/example with spaces.tf", "content")
    builder.unpack(data.getvalue(), tmp_path)
    assert (tmp_path / "tests/example with spaces.tf").read_text() == "content"


def test_cli_passes_verified_source_packages_to_isolated_worker(tmp_path, monkeypatch):
    item = provider()
    data = built_package(item)
    item["source_build"]["sha256"] = hashlib.sha256(data).hexdigest()
    builds = tmp_path / "builds"
    path = builds / item["source"] / item["version"] / "linux_amd64.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": 1, "engine": "terraform", "providers": [item], "modules": []})
    )
    output = tmp_path / "prepared"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
            "--provider-builds",
            str(builds),
        ],
    )
    prep.main()
    record = json.loads((output / "receipt.json").read_text())["providers"][0]
    assert record["sha256"] == item["source_build"]["sha256"]


def test_manifest_refresh_retains_source_builds_and_refuses_new_unbuilt_identity():
    original = provider()
    proposed = copy.deepcopy(original)
    del proposed["source_build"]
    document = {"providers": [proposed]}
    retain_source_builds(document, {"providers": [original]})
    assert proposed["source_build"] == original["source_build"]
    proposed["version"] = "999.0.0"
    with pytest.raises(ValueError, match="reviewed source-build lock"):
        retain_source_builds(document, {"providers": [original]})


def test_manifest_refresh_refuses_changed_upstream_archive():
    original = provider()
    proposed = copy.deepcopy(original)
    proposed["artifact"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="reviewed source-build lock"):
        retain_source_builds({"providers": [proposed]}, {"providers": [original]})


@pytest.mark.parametrize("field", ["modules", "module_files", "revision"])
def test_approved_package_cannot_claim_different_build_inputs(tmp_path, field):
    item = provider()
    data = built_package(item)
    target = tmp_path / item["source"] / item["version"] / "linux_amd64.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    if field == "revision":
        item["source_build"][field] = "f" * 40
    elif field == "modules":
        item["source_build"][field]["golang.org/x/net"] = "v0.99.0"
    else:
        item["source_build"][field]["go.mod"] = "f" * 64
    with pytest.raises(prep.Refused, match="artifact-mismatch"):
        prep.built_provider(item, tmp_path)


@pytest.mark.parametrize("fixture", [False, True])
def test_source_unpack_skips_symlink_fixtures_and_refuses_other_symlinks(tmp_path, fixture):
    data = io.BytesIO()
    name = "testdata/link" if fixture else "source.go"
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("source/", "")
        info = zipfile.ZipInfo("source/" + name)
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../../outside")
    if fixture:
        builder.unpack(data.getvalue(), tmp_path)
        assert not (tmp_path / name).exists()
    else:
        with pytest.raises(ValueError, match="symlink outside test fixtures"):
            builder.unpack(data.getvalue(), tmp_path)


def test_release_catalogue_requires_source_builds_with_distinct_reviewed_package_hashes():
    manifest = prep.load_manifest(
        (ROOT / "images/terraform-sandbox/dependencies.terraform-avm.json").read_bytes()
    )
    for item in manifest["providers"]:
        plan = item["source_build"]
        assert plan["sha256"] not in {"0" * 64, item["artifact"]["sha256"]}
        assert all(value != "0" * 64 for value in plan["module_files"].values())


@pytest.mark.skipif(
    os.environ.get("MAF_TEST_PROVIDER_CACHE") != "1", reason="needs MAF_TEST_PROVIDER_CACHE=1"
)
def test_provider_cache_cleanup_preserves_mounts_and_built_artifacts(tmp_path):
    recipe = (ROOT / "images/terraform-sandbox/prepared.Dockerfile").read_text()
    base = next(line for line in recipe.splitlines() if line.startswith("FROM golang:"))
    build = next(line for line in recipe.splitlines() if line.startswith("RUN --mount="))
    mounts, cleanup = build.split(" python3 ", 1)
    cleanup = cleanup.split(" && ", 1)[1]
    cache_id = uuid.uuid4().hex
    mounts = " ".join(
        part.replace("type=cache,", f"type=cache,id=provider-cache-test-{cache_id}-{i},")
        for i, part in enumerate(mounts.split())
    )
    seed = (
        "mkdir -p /go/pkg/mod/example /root/.cache/go-build/00 /builds && "
        "echo module > /go/pkg/mod/example/go.mod && "
        "chmod -R a-w /go/pkg/mod/example && "
        "echo cached > /root/.cache/go-build/00/cached-d && "
        "echo approved > /builds/provider.zip"
    )
    checks = (
        "test ! -e /go/pkg/mod/example && "
        "test ! -e /root/.cache/go-build/00/cached-d && "
        'test "$(cat /builds/provider.zip)" = approved'
    )
    dockerfile = f"{base}\n{mounts} {seed} && {cleanup} && {checks}\n"
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--no-cache",
            "--network=none",
            "--output=type=cacheonly",
            "--file=-",
            str(tmp_path),
        ],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
