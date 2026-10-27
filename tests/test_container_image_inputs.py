"""Dependency updates must reach every recipe and retain their exact source identities."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from container_image_inputs import (  # noqa: E402
    build_inputs,
    dockerfiles,
    inventory,
    stage_image,
    validate_version_line,
)
from select_image_security import affected  # noqa: E402

pytestmark = pytest.mark.workflow
DIGEST = "a" * 64


def test_every_dockerfile_is_pinned_and_has_a_scheduled_updater():
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    directories = {
        path
        for update in updates
        if update["package-ecosystem"] == "docker"
        for pattern in update.get("directories", [update.get("directory", "")])
        for path in ROOT.glob(pattern.lstrip("/"))
    }
    for path in dockerfiles(ROOT):
        assert path.parent in directories, path
        for reference in inventory(path):
            validate_version_line(reference, path)


def test_shared_wolfi_references_have_one_reviewed_digest():
    pins = {
        reference
        for path in dockerfiles(ROOT)
        for reference in inventory(path)
        if reference.startswith("cgr.dev/chainguard/wolfi-base:")
    }
    assert len(pins) == 1
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    group = next(
        update["groups"]["wolfi-runtime"]
        for update in updates
        if "wolfi-runtime" in update.get("groups", {})
    )
    assert group["patterns"] == ["chainguard/wolfi-base"]


@pytest.mark.parametrize(
    "recipe",
    [
        "bicep-sandbox/Dockerfile",
        "bicep-sandbox/source.Dockerfile",
        "drawio-sandbox/Dockerfile",
        "drawio-export/Dockerfile",
        "graphviz-sandbox/Dockerfile",
        "shell-sandbox/Dockerfile",
        "terraform-sandbox/Dockerfile",
    ],
)
def test_acas_runtime_layers_exclude_build_time_devices(recipe):
    source = (ROOT / "images" / recipe).read_text()
    final = source.rsplit("FROM scratch", 1)
    assert len(final) == 2
    assert "COPY --from=assembled --exclude=dev/* / /" in final[1]
    assert "ENV PATH=" in final[1]
    assert "SSL_CERT_FILE=" in final[1]


@pytest.mark.parametrize("name", ["source.Dockerfile", "prepared.Dockerfile"])
def test_non_default_recipes_are_retained_as_build_inputs(name):
    assert ROOT / "images/bicep-sandbox" / name in build_inputs(ROOT)
    assert affected("images/bicep-sandbox/" + name) == ("bicep", "bicep-avm", "bicep-docker-sbx")


@pytest.mark.parametrize(
    "path",
    [
        "images/bicep-sandbox/dependencies.csproj",
        "images/drawio-export/requirements.txt",
        "packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy/go.mod",
        "packages/maf-sandbox-wslc/src/maf_sandbox_wslc/_proxy/go.sum",
    ],
)
def test_native_manifests_are_hashed_and_select_consumers(path):
    assert ROOT / path in build_inputs(ROOT)
    assert affected(path)


@pytest.mark.parametrize(
    "new_tag,new_digest", [("1.27.3-alpine", DIGEST), ("1.27.2-alpine", "b" * 64)]
)
def test_tag_and_same_tag_digest_updates_change_evidence_and_select_all_engines(
    tmp_path, new_tag, new_digest
):
    context = tmp_path / "images/terraform-sandbox"
    context.mkdir(parents=True)
    recipe = context / "prepared.Dockerfile"
    recipe.write_text(
        f"FROM golang:1.27.2-alpine@sha256:{DIGEST} AS provider-toolchain\nFROM provider-toolchain AS build\n"
    )
    before = hashlib.sha256(recipe.read_bytes()).digest()
    recipe.write_text(
        f"FROM golang:{new_tag}@sha256:{new_digest} AS provider-toolchain\nFROM provider-toolchain AS build\n"
    )
    assert hashlib.sha256(recipe.read_bytes()).digest() != before
    assert inventory(recipe) == [f"golang:{new_tag}@sha256:{new_digest}"]
    assert recipe in build_inputs(tmp_path)
    assert set(affected("images/terraform-sandbox/prepared.Dockerfile")) == {
        "terraform-random",
        "opentofu-random",
        "terraform-avm",
        "opentofu-avm",
    }


@pytest.mark.parametrize(
    "reference", ["alpine:3.24", "python:latest", "${UNKNOWN}", "ubuntu@sha256:" + DIGEST]
)
def test_unpinned_or_hidden_external_inputs_are_refused(tmp_path, reference):
    recipe = tmp_path / "Dockerfile"
    recipe.write_text(f"FROM {reference}\n")
    with pytest.raises(ValueError):
        inventory(recipe)


def test_stages_supplied_parents_and_python_heredocs_are_not_external_images(tmp_path):
    recipe = tmp_path / "Dockerfile"
    recipe.write_text(
        f"ARG BASE_IMAGE=scratch\nFROM alpine:3.24@sha256:{DIGEST} AS build\n"
        "RUN python - <<'PY'\nfrom pathlib import Path\nPY\n"
        "FROM build AS second\nFROM ${BASE_IMAGE}\n"
    )
    assert inventory(recipe) == [f"alpine:3.24@sha256:{DIGEST}"]


@pytest.mark.parametrize(
    "reference",
    [
        "python:3.14-slim-trixie",
        "python:3.13-slim-bookworm",
        "golang:1.28.0-alpine",
        "mcr.microsoft.com/dotnet/sdk:11.0.100",
    ],
)
def test_compatibility_family_changes_require_policy_review(reference):
    with pytest.raises(ValueError, match="compatibility"):
        validate_version_line(reference + "@sha256:" + DIGEST, Path("images/Dockerfile"))


def test_sdk_stage_must_be_literal_and_immutable(tmp_path):
    path = tmp_path / "source.Dockerfile"
    path.write_text("ARG SDK_IMAGE=scratch\nFROM ${SDK_IMAGE} AS build\n")
    with pytest.raises(ValueError, match="pinned literal"):
        stage_image(path, "build")


def test_external_copy_sources_require_a_tagged_digest(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("FROM scratch\nCOPY --from=alpine:latest /etc /etc\n")
    with pytest.raises(ValueError, match="COPY source"):
        inventory(path)
    path.write_text(f"FROM scratch\nCOPY --from=alpine:3.24@sha256:{DIGEST} /etc /etc\n")
    assert inventory(path) == [f"alpine:3.24@sha256:{DIGEST}"]


def test_json_manifests_cannot_own_image_pins():
    for name, keys in [
        ("shell-sandbox/image.json", {"baseImage"}),
        ("terraform-sandbox/image.json", {"base_image"}),
        ("bicep-sandbox/source.json", {"sdkImage", "sdkVersion", "packages"}),
    ]:
        assert not keys & json.loads((ROOT / "images" / name).read_text()).keys()


def test_proxy_build_consumes_native_manifests_without_mutating_them():
    for package in ("docker", "wslc"):
        context = ROOT / f"packages/maf-sandbox-{package}/src/maf_sandbox_{package}/_proxy"
        recipe = (context / "Dockerfile").read_text()
        assert "COPY go.mod go.sum /src/" in recipe
        assert "go get " not in recipe
        assert "GOFLAGS=-mod=readonly" in recipe
