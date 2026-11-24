"""Binding checks for the upstream plugin archive and the trusted release writer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hyperlight_plugin_evidence as evidence
from container_release import digest, write

ROOT = Path(__file__).resolve().parents[1]


def make_oci_layout(layout, monkeypatch, fault):
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def put(document):
        raw = json.dumps(document).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (blobs / sha).write_bytes(raw)
        return {"digest": "sha256:" + sha, "size": len(raw)}

    platform = put({"schemaVersion": 2, "mediaType": evidence.MANIFEST})
    platform.update(mediaType=evidence.MANIFEST, platform={"architecture": "amd64", "os": "linux"})
    amd64 = platform["digest"]
    monkeypatch.setattr(evidence, "AMD64_DIGEST", amd64)
    layers = []
    expected = {"linux-amd64.json": amd64}
    for kind, name in [
        (evidence.PREDICATE, "buildkit-provenance.json"),
        (evidence.SPDX, "sbom.json"),
    ]:
        subject = "0" * 64 if fault == (name, "subject") else amd64[7:]
        descriptor = put(
            {
                "predicateType": "wrong" if fault == (name, "predicate") else kind,
                "subject": [{"digest": {"sha256": subject}}],
            }
        )
        descriptor["annotations"] = {"in-toto.io/predicate-type": kind}
        expected[name] = descriptor["digest"]
        if fault != (name, "missing"):
            layers.append(descriptor)
        if fault == (name, "duplicate"):
            layers.append(descriptor)
    manifest = put(
        {
            "subject": {"digest": "wrong" if fault == ("manifest", "subject") else amd64},
            "layers": layers,
        }
    )
    expected["attestation-manifest.json"] = manifest["digest"]
    manifest["annotations"] = {
        "vnd.docker.reference.digest": "wrong" if fault == ("index", "reference") else amd64,
        "vnd.docker.reference.type": "attestation-manifest",
    }
    index = put({"mediaType": evidence.INDEX, "manifests": [platform, manifest]})
    expected["signed-manifest.json"] = index["digest"]
    monkeypatch.setattr(evidence, "INDEX_DIGEST", index["digest"])
    if fault and fault[1] == "substitute":
        path = blobs / expected[fault[0]][7:]
        path.write_bytes(b"x" * path.stat().st_size)
    return expected


@pytest.mark.parametrize(
    "fault",
    [
        None,
        ("index", "reference"),
        ("manifest", "subject"),
        *[
            (name, "substitute")
            for name in (
                "signed-manifest.json",
                "linux-amd64.json",
                "attestation-manifest.json",
                "buildkit-provenance.json",
                "sbom.json",
            )
        ],
        *[
            (name, defect)
            for name in ("buildkit-provenance.json", "sbom.json")
            for defect in ("subject", "predicate", "missing", "duplicate")
        ],
    ],
)
def test_collects_only_statements_bound_to_the_reviewed_image(tmp_path, monkeypatch, fault):
    expected = make_oci_layout(tmp_path / "layout", monkeypatch, fault)
    files = tmp_path / "evidence"
    files.mkdir()
    if fault is not None:
        with pytest.raises(ValueError):
            evidence.collect_manifests(tmp_path / "layout", files)
        return
    evidence.collect_manifests(tmp_path / "layout", files)
    assert {path.name: digest(path) for path in files.iterdir()} == expected


@pytest.mark.parametrize("case", ["accepted", "cli-error", "empty", "wrong-name", "wrong-digest"])
def test_signature_enforces_publisher_source_and_signed_subject(tmp_path, monkeypatch, case):
    subject = {
        "name": "other" if case == "wrong-name" else evidence.IMAGE,
        "digest": {"sha256": "0" * 64 if case == "wrong-digest" else evidence.INDEX_DIGEST[7:]},
    }
    records = (
        [] if case == "empty" else [{"verificationResult": {"statement": {"subject": [subject]}}}]
    )
    response = json.dumps(records).encode()
    cli = Mock(return_value=response)
    if case == "cli-error":
        cli.side_effect = subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(evidence, "gh", cli)
    result = tmp_path / "verification.json"
    if case == "accepted":
        evidence.verify_signature(tmp_path)
        assert result.read_bytes() == response
    else:
        with pytest.raises(subprocess.CalledProcessError if case == "cli-error" else ValueError):
            evidence.verify_signature(tmp_path)
        assert not result.exists()
    cli.assert_called_once_with(
        tmp_path,
        "attestation",
        "verify",
        "signed-manifest.json",
        "--bundle",
        "attestation-bundles.jsonl",
        "--custom-trusted-root",
        "trusted-root.jsonl",
        "--repo",
        "hyperlight-dev/hyperlight-on-kubernetes",
        "--hostname",
        "github.com",
        "--cert-identity",
        "https://github.com/hyperlight-dev/hyperlight-on-kubernetes/.github/workflows/publish-device-plugin.yml@refs/heads/main",
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--source-ref",
        "refs/heads/main",
        "--source-digest",
        evidence.SOURCE,
        "--deny-self-hosted-runners",
        "--predicate-type",
        "https://slsa.dev/provenance/v1",
        "--format",
        "json",
    )


def test_failed_signature_leaves_no_collect_archive(tmp_path, monkeypatch):
    def gh(files, *args):
        if args[:2] == ("attestation", "download"):
            (files / "sha256-bundle.jsonl").write_bytes(b"bundle")
        if args[:2] == ("attestation", "verify"):
            raise subprocess.CalledProcessError(1, "gh attestation verify")
        return b"fixture"

    monkeypatch.setattr(evidence.subprocess, "run", Mock())
    monkeypatch.setattr(evidence, "collect_manifests", Mock())
    monkeypatch.setattr(evidence, "verify_materials", Mock())
    monkeypatch.setattr(evidence, "gh", gh)
    (tmp_path / evidence.ARCHIVE).write_bytes(b"previous run")
    with pytest.raises(subprocess.CalledProcessError):
        evidence.collect(tmp_path)
    assert not (tmp_path / evidence.ARCHIVE).exists()


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
