"""An independent receipt protects unsigned evidence as well as signed provenance."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import sys
import tarfile
import zipfile
from contextlib import nullcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hyperlight_evidence as evidence
import verify_hyperlight_aks_image as producer
import verify_hyperlight_retained_evidence as retained

SIGNER = "https://github.com/example/build/.github/workflows/sign.yml@refs/heads/main"
BUNDLE = '{"checkpoint":{"envelope":"signed header\\n\u2014 signer\\n\\n"}}\r\n'.encode()


def encoded(value):
    return json.dumps(value).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pack(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.fixture
def case(tmp_path, monkeypatch):
    source = {"repository": producer.SOURCE_URL, "revision": "a" * 40, "dirty": False}
    files = {"source.json": encoded(source)}
    files["build-inputs.json"] = encoded({"source.json": digest(files["source.json"])})
    layer_bytes = io.BytesIO()
    with tarfile.open(fileobj=layer_bytes, mode="w:gz") as layer:
        for name, data in files.items():
            member = tarfile.TarInfo("opt/" + name)
            member.size = len(data)
            layer.addfile(member, io.BytesIO(data))
    files["metadata-layer.tar.gz"] = layer_bytes.getvalue()
    files["signed-manifest.json"] = encoded(
        {"layers": [{"digest": "sha256:" + digest(files["metadata-layer.tar.gz"])}]}
    )
    candidate = {
        "image": "registry.example/runtime@sha256:" + digest(files["signed-manifest.json"]),
        "signer_identity": SIGNER,
        "source_revision": source["revision"],
        "source_ref": "refs/heads/main",
        "build_inputs_sha256": digest(files["build-inputs.json"]),
    }
    files.update(
        {
            "attestation-bundles.jsonl": BUNDLE,
            "trusted-root.jsonl": b"approved roots\n",
            "promotion.json": encoded(
                {
                    "verifications": [
                        {
                            "image": candidate["image"],
                            "registry_digest": candidate["image"].split("@")[1],
                            "policy": candidate,
                        }
                    ]
                }
            ),
            "acceptance.json": encoded({"candidate": candidate, "passed": True}),
            "node-pull.json": encoded(
                {
                    "image": candidate["image"],
                    "smoke": {
                        "source": source,
                        "build_inputs_sha256": candidate["build_inputs_sha256"],
                    },
                }
            ),
        }
    )
    for name in retained.REQUIRED - files.keys():
        files[name] = encoded({"retained": True})
    files["SHA256SUMS.json"] = encoded({name: digest(data) for name, data in files.items()})
    archive = tmp_path / "archive.zip"
    pack(archive, files)
    expected = {
        "candidate": candidate,
        "archive_sha256": evidence.sha(archive),
        "trusted_root_sha256": digest(files["trusted-root.jsonl"]),
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(encoded(expected))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["gh", "attestation", "verify"]
        assert not command[3].startswith("oci:")
        assert "--bundle" in command and "--custom-trusted-root" in command
        assert "--deny-self-hosted-runners" in command
        if command[command.index("--cert-identity") + 1] != SIGNER or (
            Path(command[command.index("--bundle") + 1]).read_bytes() != BUNDLE
        ):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {
                        "verificationResult": {
                            "statement": {
                                "predicateType": evidence.PREDICATE,
                                "subject": [
                                    {
                                        "digest": {
                                            "sha256": candidate["image"].split("@sha256:")[1]
                                        },
                                    }
                                ],
                            },
                        }
                    }
                ]
            ),
        )

    monkeypatch.setattr(evidence.subprocess, "run", run)
    return archive, receipt, files, expected, calls


def test_complete_archive(case):
    archive, receipt, _, _, calls = case
    result = retained.verify(archive, receipt)
    assert result["signed_provenance_verified"] is True
    assert result["metadata_bound_to_signed_manifest"] is True
    assert len(calls) == 1


def replace_promotion(case, verifications):
    archive, receipt, files, expected, _ = case
    files["promotion.json"] = encoded({"verifications": verifications})
    files["SHA256SUMS.json"] = encoded(
        {name: digest(data) for name, data in files.items() if name != "SHA256SUMS.json"}
    )
    pack(archive, files)
    expected["archive_sha256"] = evidence.sha(archive)
    receipt.write_bytes(encoded(expected))


@pytest.mark.parametrize("position", [0, 1, 7])
def test_promotion_selects_receipt_candidate_at_any_position(case, position):
    archive, receipt, files, expected, _ = case
    selected = json.loads(files["promotion.json"])["verifications"][0]
    others = [
        {**selected, "image": f"registry.example/other-{index}@sha256:" + "b" * 64}
        for index in range(7)
    ]
    others.insert(position, selected)
    replace_promotion(case, others)
    result = retained.verify(archive, receipt)
    assert result["image_digest"] == expected["candidate"]["image"].split("@")[1]


@pytest.mark.parametrize("kind", ["empty", "missing", "duplicate", "conflicting-duplicate"])
def test_promotion_requires_one_candidate_match(case, kind):
    archive, receipt, files, _, _ = case
    selected = json.loads(files["promotion.json"])["verifications"][0]
    other = {**selected, "image": "registry.example/other@sha256:" + "b" * 64}
    records = {
        "empty": [],
        "missing": [other],
        "duplicate": [selected, selected],
        "conflicting-duplicate": [selected, {**selected, "registry_digest": "sha256:" + "b" * 64}],
    }[kind]
    replace_promotion(case, records)
    with pytest.raises(ValueError, match="exactly one"):
        retained.verify(archive, receipt)


@pytest.mark.parametrize("records", [None, {}, [None]])
def test_promotion_rejects_malformed_verifications(case, records):
    archive, receipt, _, _, _ = case
    replace_promotion(case, records)
    with pytest.raises(ValueError, match="list of records"):
        retained.verify(archive, receipt)


@pytest.mark.parametrize(
    "field",
    ["registry_digest", "source_revision", "source_ref", "build_inputs_sha256", "signer_identity"],
)
def test_selected_promotion_still_requires_expected_digest_and_policy(case, field):
    archive, receipt, files, _, _ = case
    selected = json.loads(files["promotion.json"])["verifications"][0]
    other = {**selected, "image": "registry.example/other@sha256:" + "b" * 64}
    if field == "registry_digest":
        selected[field] = "sha256:" + "b" * 64
        message = "promotion image association differs"
    else:
        selected["policy"] = {**selected["policy"], field: "mismatched"}
        message = "promotion policy association differs: " + field
    replace_promotion(case, [other, selected])
    with pytest.raises(ValueError, match=message):
        retained.verify(archive, receipt)


@pytest.mark.parametrize("mutation", ["hold", "policy", "acceptance"])
def test_recomputed_inventory_cannot_replace_independent_archive_digest(case, mutation):
    archive, receipt, files, expected, calls = case
    if mutation == "hold":
        del files["retention-hold.json"]
    elif mutation == "policy":
        files["retention-policy.json"] = b"{}"
    else:
        files["acceptance.json"] = encoded({"candidate": expected["candidate"]})
    files["SHA256SUMS.json"] = encoded(
        {name: digest(data) for name, data in files.items() if name != "SHA256SUMS.json"}
    )
    pack(archive, files)
    with pytest.raises(ValueError, match="archive hash"):
        retained.verify(archive, receipt)
    assert not calls


@pytest.mark.parametrize("value", [None, "", "a" * 63, "A" * 64, 123, {}, "sha256:" + "a" * 64])
def test_invalid_receipt_hash(case, value):
    archive, receipt, _, expected, calls = case
    expected["archive_sha256"] = value
    receipt.write_bytes(encoded(expected))
    with pytest.raises(ValueError, match="expected hash"):
        retained.verify(archive, receipt)
    assert not calls


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/b", "a\\b", "x:stream", "CON", "nul.txt", "a."]
)
def test_unsafe_members_refused_before_extraction(case, name):
    archive, _, files, _, _ = case
    files[name] = b"bad"
    pack(archive, files)
    destination = archive.parent / "extracted"
    destination.mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        evidence.extract_archive(archive, destination, evidence.sha(archive))
    assert not list(destination.iterdir())


@pytest.mark.parametrize("kind", ["duplicate", "case", "symlink", "directory"])
def test_duplicate_or_special_members(case, kind):
    archive, _, _, _, _ = case
    with zipfile.ZipFile(archive, "a") as source:
        if kind in {"duplicate", "case"}:
            with pytest.warns(UserWarning) if kind == "duplicate" else nullcontext():
                source.writestr("source.json" if kind == "duplicate" else "SOURCE.JSON", b"x")
        else:
            info = zipfile.ZipInfo("link" if kind == "symlink" else "directory/")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            source.writestr(info, b"source.json")
    with pytest.raises(ValueError, match="unsafe"):
        evidence.extract_archive(archive, archive.parent, evidence.sha(archive))


@pytest.mark.parametrize("missing", sorted(retained.REQUIRED))
def test_missing_required_evidence_even_in_newly_approved_archive(case, missing):
    archive, receipt, files, expected, calls = case
    del files[missing]
    files["SHA256SUMS.json"] = encoded(
        {name: digest(data) for name, data in files.items() if name != "SHA256SUMS.json"}
    )
    pack(archive, files)
    expected["archive_sha256"] = evidence.sha(archive)
    receipt.write_bytes(encoded(expected))
    with pytest.raises(ValueError, match="required evidence"):
        retained.verify(archive, receipt)
    assert not calls


def test_missing_archive(case):
    archive, receipt, _, _, calls = case
    archive.unlink()
    with pytest.raises(FileNotFoundError):
        retained.verify(archive, receipt)
    assert not calls


@pytest.mark.parametrize(
    "field",
    [
        "image",
        "signer_identity",
        "source_revision",
        "source_ref",
        "build_inputs_sha256",
    ],
)
def test_offline_empty_policy_fields_are_refused(case, field):
    archive, receipt, _, expected, calls = case
    expected["candidate"][field] = ""
    receipt.write_bytes(encoded(expected))
    with pytest.raises(ValueError):
        retained.verify(archive, receipt)
    assert not calls


@pytest.mark.parametrize("change", ["signer", "bundle", "trust"])
def test_offline_signature_policy_and_trust_are_independent(case, change):
    archive, receipt, files, expected, _ = case
    if change == "signer":
        expected["candidate"]["signer_identity"] = SIGNER.replace("sign.yml", "other.yml")
    else:
        files["attestation-bundles.jsonl" if change == "bundle" else "trusted-root.jsonl"] += b"bad"
        files["SHA256SUMS.json"] = encoded(
            {name: digest(data) for name, data in files.items() if name != "SHA256SUMS.json"}
        )
        pack(archive, files)
        expected["archive_sha256"] = evidence.sha(archive)
    receipt.write_bytes(encoded(expected))
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        retained.verify(archive, receipt)


@pytest.mark.parametrize("failure", [None, "manifest", "download", "bundle", "offline", "publish"])
def test_producer_preserves_original_bytes_and_removes_stale_pair(case, monkeypatch, failure):
    archive, _, files, expected, _ = case
    output = archive.parent / "verification.json"
    sidecar = evidence.sidecar_path(output)
    output.write_text("stale success")
    sidecar.write_bytes(b"stale bundle")
    offline_run = evidence.subprocess.run

    def online(**kwargs):
        return {
            "attestations": [{"bundle": "parsed diagnostic representation"}],
            "signed_provenance_verified": True,
        }

    def run(command, **kwargs):
        if command[0] == "docker":
            data = b"wrong" if failure == "manifest" else files["signed-manifest.json"]
            return subprocess.CompletedProcess(command, 0, data)
        if command[2] == "download":
            if failure != "download":
                Path(kwargs["cwd"], "sha256-original.jsonl").write_bytes(
                    b"broken" if failure == "bundle" else BUNDLE
                )
            return subprocess.CompletedProcess(command, 0)
        if command[2] == "trusted-root":
            return subprocess.CompletedProcess(command, 0, files["trusted-root.jsonl"])
        if failure == "offline":
            raise subprocess.CalledProcessError(1, command)
        return offline_run(command, **kwargs)

    monkeypatch.setattr(producer, "_verify_published_image", online)
    monkeypatch.setattr(producer.subprocess, "run", run)
    if failure == "publish":
        replace = producer.os.replace

        def refuse_report(source, destination):
            if destination == output:
                raise OSError("publish failed")
            replace(source, destination)

        monkeypatch.setattr(producer.os, "replace", refuse_report)
    if failure:
        with pytest.raises((ValueError, OSError, subprocess.CalledProcessError)):
            producer.verify_published_image(**expected["candidate"], output=output)
        assert not output.exists() and not sidecar.exists()
    else:
        report = producer.verify_published_image(**expected["candidate"], output=output)
        with zipfile.ZipFile(sidecar) as saved:
            assert saved.read("attestation-bundles.jsonl") == BUNDLE
            assert saved.read("signed-manifest.json") == files["signed-manifest.json"]
            assert saved.read("trusted-root.jsonl") == files["trusted-root.jsonl"]
        saved_record = report["retained_evidence"]
        assert isinstance(saved_record, dict)
        assert saved_record["archive_sha256"] == evidence.sha(sidecar)
        assert saved_record["offline_verified"] is True
