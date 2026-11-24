"""Binding checks for the upstream plugin archive and the trusted release writer."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hyperlight_plugin_evidence as evidence
from container_release import digest, write

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("change", ["none", "base", "floating", "source", "stage"])
def test_both_pinned_stages_must_match_signed_build_materials(tmp_path, change):
    sha = "a" * 64
    bases = [("golang", "1.27-alpine"), ("alpine", "3.24")]
    dockerfile = [f"FROM {name}:{tag}@sha256:{sha}" for name, tag in bases]
    dependencies = [
        {
            "uri": f"pkg:docker/{name}@{tag}?digest=sha256:{sha}&platform=linux%2Famd64",
            "digest": {"sha256": sha},
        }
        for name, tag in bases
    ]
    if change == "base":
        dependencies[0]["digest"] = {"sha256": "b" * 64}
    if change == "floating":
        dockerfile[1] = "FROM alpine:3.24"
    if change == "stage":
        dockerfile.pop()
    (tmp_path / "Dockerfile").write_text("\n".join(dockerfile))
    args = {
        "vcs:revision": "wrong" if change == "source" else evidence.SOURCE,
        "vcs:source": f"https://github.com/{evidence.UPSTREAM}",
    }
    write(
        tmp_path / "buildkit-provenance.json",
        {
            "predicate": {
                "buildDefinition": {
                    "externalParameters": {"request": {"root": {"request": {"args": args}}}},
                    "resolvedDependencies": dependencies,
                }
            }
        },
    )
    if change == "none":
        evidence.verify_materials(tmp_path)
    else:
        with pytest.raises(ValueError):
            evidence.verify_materials(tmp_path)


def test_retain_requires_the_transferred_hash_and_reads_back_before_publication(
    tmp_path, monkeypatch
):
    archive = tmp_path / evidence.ARCHIVE
    archive.write_bytes(b"verified")
    writer = Mock()
    monkeypatch.setattr(evidence, "Evidence", lambda: writer)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "10")
    monkeypatch.setenv("EVIDENCE_ATTEMPT", "2")
    with pytest.raises(ValueError, match="Transferred evidence"):
        evidence.publish(tmp_path, "sha256:" + "0" * 64)
    writer.ensure.assert_not_called()
    writer.ensure.return_value = {"id": 1}
    writer.github.assets.return_value = [{"name": evidence.ARCHIVE}, {"name": "unexpected"}]
    with pytest.raises(ValueError, match="Unexpected assets"):
        evidence.publish(tmp_path, digest(archive))
    writer.publish.assert_not_called()
    writer.github.assets.return_value = [{"name": evidence.ARCHIVE}]
    evidence.publish(tmp_path, digest(archive))
    writer.ensure.assert_called_with(
        f"image-hyperlight-device-plugin-{evidence.INDEX_DIGEST[7:]}-10-2", "a" * 40
    )
    writer.retain.assert_called_with({"id": 1}, tmp_path, {evidence.ARCHIVE: digest(archive)})
    writer.publish.assert_called_once_with({"id": 1}, "a" * 40)


def test_only_trusted_main_can_write_release_assets():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/hyperlight-plugin-evidence.yml").read_text("utf-8")
    )
    verify, retain = (workflow["jobs"][key] for key in ("verify", "retain"))
    assert verify["permissions"] == {"contents": "read", "attestations": "read"}
    assert retain["needs"] == "verify"
    assert retain["if"] == (
        "github.repository == 'sokolaidev/maf-extensions' && github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
