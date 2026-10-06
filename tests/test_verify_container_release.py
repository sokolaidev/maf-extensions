"""Each attestation is necessary; signed but mismatched completion cannot authorize execution."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from container_release import (  # noqa: E402
    COMPLETION,
    PREFIX,
    PROVENANCE,
    SIGNER,
    SPDX,
    digest,
    write,
)
from container_release_oci import MANIFEST  # noqa: E402
from verify_container_release import (  # noqa: E402
    policy_flags,
    validate_completion,
    verify,
    verify_evidence,
)


def evidence(root, profile="bicep"):
    expected = {
        "profile": profile,
        "version": "0.1.0",
        "sourceCommit": "c" * 40,
        "sourceRef": "refs/heads/main",
        "imageId": "sha256:" + "d" * 64,
        "attemptId": "123",
    }
    write(
        root / "manifest.json",
        {"schemaVersion": 2, "mediaType": MANIFEST, "config": {"digest": expected["imageId"]}},
    )
    expected["registryDigest"] = expected["assessedManifestDigest"] = digest(root / "manifest.json")
    spdx = {"spdxVersion": "SPDX-2.3", "packages": [{"name": "component"}]}
    write(root / "sbom.spdx.json", spdx)
    files = {
        "manifest.json",
        "sbom.spdx.json",
        "build.json",
        "sbom.syft.json",
        "grype.json",
        "runtime.json",
        "provenance.jsonl",
        "sbom.jsonl",
    }
    for filename in files - {"manifest.json", "sbom.spdx.json"}:
        write(root / filename, {})
    if profile == "hyperlight":
        write(root / "hyperlight-build-inputs.json", {"wheels/runtime.whl": "a" * 64})
        files.add("hyperlight-build-inputs.json")
        expected["buildInputsSha256"] = digest(root / "hyperlight-build-inputs.json")
    write(root / "evidence-index.json", {name: digest(root / name) for name in files})
    expected["evidenceIndexSha256"] = digest(root / "evidence-index.json")
    completion = {"schemaVersion": 1, "state": "completed"} | {
        field: expected[field]
        for field in (
            "profile",
            "version",
            "sourceCommit",
            "sourceRef",
            "registryDigest",
            "assessedManifestDigest",
            "evidenceIndexSha256",
        )
    }
    provenance = {
        "buildDefinition": {
            "resolvedDependencies": [
                {
                    "uri": "git+https://github.com/sokolaidev/maf-extensions@refs/heads/main",
                    "digest": {"gitCommit": expected["sourceCommit"]},
                }
            ]
        }
    }
    claims = {PROVENANCE: provenance, SPDX: spdx, COMPLETION: completion}
    result = {
        predicate: [
            {
                "verificationResult": {
                    "statement": {
                        "_type": "https://in-toto.io/Statement/v1",
                        "predicateType": predicate,
                        "subject": [
                            {
                                "name": PREFIX + "/" + profile,
                                "digest": {"sha256": expected["registryDigest"][7:]},
                            }
                        ],
                        "predicate": claim,
                    }
                }
            }
        ]
        for predicate, claim in claims.items()
    }
    return expected, result


def mock_verifier(monkeypatch, results, failed=None):
    calls = []

    def run(command, **kwargs):
        assert command[:3] == ["gh", "attestation", "verify"]
        assert kwargs["check"] is True
        assert "--deny-self-hosted-runners" in command
        assert command[command.index("--cert-identity") + 1] == SIGNER
        predicate = command[command.index("--predicate-type") + 1]
        calls.append(predicate)
        if predicate == failed:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, json.dumps(results[predicate]))

    monkeypatch.setattr("verify_container_release.subprocess.run", run)
    return calls


def test_all_three_attestations_are_verified_separately(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    calls = mock_verifier(monkeypatch, results)
    result = verify(expected, tmp_path)
    assert calls == [PROVENANCE, PROVENANCE, SPDX, SPDX, COMPLETION]
    assert result["releaseIdentityVerified"] is True
    assert result["monitoringStatus"] == "not-queried"


def test_registry_retrieval_pins_the_same_digest_and_all_three_predicates(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    targets = []

    def run(command, **kwargs):
        if "--bundle" not in command:
            targets.append(command[3])
            assert "--bundle-from-oci" in command
        else:
            assert command[3] == str(tmp_path / "manifest.json")
        assert command[command.index("--signer-digest") + 1] == expected["sourceCommit"]
        predicate = command[command.index("--predicate-type") + 1]
        return subprocess.CompletedProcess(command, 0, json.dumps(results[predicate]))

    monkeypatch.setattr("verify_container_release.subprocess.run", run)
    assert verify(expected, tmp_path, registry=True)["releaseIdentityVerified"]
    assert targets == [f"oci://{PREFIX}/bicep@{expected['registryDigest']}"] * 3


def test_hyperlight_payload_runs_only_after_all_three_attestations(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path, "hyperlight")
    calls = mock_verifier(monkeypatch, results)

    def payload(policy, directory):
        assert calls == [PROVENANCE, PROVENANCE, SPDX, SPDX, COMPLETION]
        assert policy == expected and directory == tmp_path
        return {"checked": True}

    monkeypatch.setattr("verify_container_release.hyperlight_payload", payload)
    assert verify(expected, tmp_path)["hyperlightPayloadVerification"] == {"checked": True}


@pytest.mark.parametrize("failed", [PROVENANCE, SPDX, COMPLETION])
def test_failed_signature_never_runs_hyperlight_payload(tmp_path, monkeypatch, failed):
    expected, results = evidence(tmp_path, "hyperlight")
    mock_verifier(monkeypatch, results, failed)
    monkeypatch.setattr(
        "verify_container_release.hyperlight_payload",
        lambda *_: pytest.fail("Image code executed before release authentication"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        verify(expected, tmp_path)


@pytest.mark.parametrize("change", ["source", "dirty", "image", "inputs"])
def test_hyperlight_payload_keeps_source_and_build_input_checks(tmp_path, monkeypatch, change):
    from verify_container_release import hyperlight_payload

    expected, _ = evidence(tmp_path, "hyperlight")
    source = {
        "repository": "https://github.com/sokolaidev/maf-extensions",
        "revision": expected["sourceCommit"],
        "dirty": False,
    }
    if change == "source":
        source["revision"] = "f" * 40
    elif change == "dirty":
        source["dirty"] = True
    elif change == "inputs":
        expected["buildInputsSha256"] = "sha256:" + "f" * 64

    def run(command, **kwargs):
        assert command[0] == "docker"
        image_id = expected["imageId"] if change != "image" else "sha256:" + "f" * 64
        return subprocess.CompletedProcess(command, 0, stdout=image_id)

    monkeypatch.setattr("verify_container_release.subprocess.run", run)
    monkeypatch.setattr("verify_container_release.verify_image", lambda *_: {"source": source})
    with pytest.raises(ValueError):
        hyperlight_payload(expected, tmp_path)


@pytest.mark.parametrize("failed", [PROVENANCE, SPDX, COMPLETION])
def test_any_failed_signature_blocks_identity(tmp_path, monkeypatch, failed):
    expected, results = evidence(tmp_path)
    mock_verifier(monkeypatch, results, failed)
    with pytest.raises(subprocess.CalledProcessError):
        verify(expected, tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schemaVersion", True),
        ("schemaVersion", 2),
        ("state", "incomplete"),
        ("state", "abandoned"),
        ("profile", "diagram"),
        ("version", "0.2.0"),
        ("sourceCommit", "d" * 40),
        ("sourceRef", "refs/heads/other"),
        ("registryDigest", "sha256:" + "e" * 64),
        ("assessedManifestDigest", "sha256:" + "e" * 64),
        ("evidenceIndexSha256", "sha256:" + "e" * 64),
    ],
)
def test_valid_signature_does_not_override_completion_policy(tmp_path, monkeypatch, field, value):
    expected, results = evidence(tmp_path)
    results[COMPLETION][0]["verificationResult"]["statement"]["predicate"][field] = value
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError):
        verify(expected, tmp_path)


@pytest.mark.parametrize("predicate", [PROVENANCE, SPDX, COMPLETION])
def test_signed_wrong_subject_cannot_be_substituted(tmp_path, monkeypatch, predicate):
    expected, results = evidence(tmp_path)
    results[predicate][0]["verificationResult"]["statement"]["subject"][0]["name"] = (
        PREFIX + "/diagram"
    )
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError, match="subject"):
        verify(expected, tmp_path)


def test_payload_source_must_match_provenance_material(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    results[PROVENANCE][0]["verificationResult"]["statement"]["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ][0]["digest"]["gitCommit"] = "e" * 40
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError, match="payload source"):
        verify(expected, tmp_path)


def test_policy_pins_both_workflow_and_payload_revisions(tmp_path):
    expected, _ = evidence(tmp_path)
    flags = policy_flags(expected, COMPLETION)
    for name in ("--source-digest", "--signer-digest"):
        assert flags[flags.index(name) + 1] == expected["sourceCommit"]


def test_signed_index_cannot_hide_a_changed_scan(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    write(tmp_path / "grype.json", {"matches": []})
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError, match="Changed or missing"):
        verify(expected, tmp_path)


def test_empty_success_from_cli_is_not_verification(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    results[COMPLETION] = []
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError, match="No authenticated"):
        verify(expected, tmp_path)


def test_unsigned_catalogue_is_not_an_attestation(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    results[COMPLETION] = [copy.deepcopy(expected)]
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError):
        verify(expected, tmp_path)


def test_missing_completion_field_is_refused(tmp_path):
    expected, results = evidence(tmp_path)
    claim = results[COMPLETION][0]["verificationResult"]["statement"]["predicate"]
    del claim["evidenceIndexSha256"]
    with pytest.raises(ValueError):
        validate_completion(claim, expected)


def test_index_path_escape_is_refused(tmp_path):
    expected, _ = evidence(tmp_path)
    index = json.loads((tmp_path / "evidence-index.json").read_text())
    index["../outside"] = expected["registryDigest"]
    write(tmp_path / "evidence-index.json", index)
    with pytest.raises(ValueError, match="Invalid or circular"):
        verify_evidence(tmp_path, digest(tmp_path / "evidence-index.json"))


@pytest.mark.parametrize("registry", [False, True])
def test_rerun_attestations_select_authenticated_retained_evidence(tmp_path, monkeypatch, registry):
    expected, retained = evidence(tmp_path)
    remote = copy.deepcopy(retained)
    for predicate in (PROVENANCE, SPDX):
        duplicate = copy.deepcopy(remote[predicate][0])
        claim = duplicate["verificationResult"]["statement"]["predicate"]
        if predicate == PROVENANCE:
            claim["runDetails"] = {"metadata": {"invocationId": "rerun-attempt-2"}}
        else:
            claim["creationInfo"] = {"created": "2026-10-06T00:00:00Z"}
        remote[predicate].append(duplicate)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        predicate = command[command.index("--predicate-type") + 1]
        results = retained if "--bundle" in command else remote
        return subprocess.CompletedProcess(command, 0, json.dumps(results[predicate]))

    monkeypatch.setattr("verify_container_release.subprocess.run", run)
    assert verify(expected, tmp_path, registry=registry)["releaseIdentityVerified"]
    assert sum("--bundle" in command for command in calls) == 2


def test_conflicting_completions_still_fail(tmp_path, monkeypatch):
    expected, results = evidence(tmp_path)
    duplicate = copy.deepcopy(results[COMPLETION][0])
    duplicate["verificationResult"]["statement"]["predicate"]["evidenceIndexSha256"] = (
        "sha256:" + "e" * 64
    )
    results[COMPLETION].append(duplicate)
    mock_verifier(monkeypatch, results)
    with pytest.raises(ValueError, match="Conflicting authenticated"):
        verify(expected, tmp_path)


@pytest.mark.parametrize("fault", ["missing-claim", "signature", "changed-bundle", "wrong-subject"])
def test_retained_selection_cannot_bypass_authentication_or_completion(
    tmp_path, monkeypatch, fault
):
    expected, retained = evidence(tmp_path)
    remote = copy.deepcopy(retained)
    if fault == "missing-claim":
        remote[PROVENANCE][0]["verificationResult"]["statement"]["predicate"]["other"] = True
    elif fault == "changed-bundle":
        write(tmp_path / "provenance.jsonl", {"substituted": True})
    elif fault == "wrong-subject":
        duplicate = copy.deepcopy(remote[PROVENANCE][0])
        duplicate["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "f" * 64
        remote[PROVENANCE].append(duplicate)

    def run(command, **kwargs):
        predicate = command[command.index("--predicate-type") + 1]
        if fault == "signature" and "--bundle" in command:
            raise subprocess.CalledProcessError(1, command)
        results = retained if "--bundle" in command else remote
        return subprocess.CompletedProcess(command, 0, json.dumps(results[predicate]))

    monkeypatch.setattr("verify_container_release.subprocess.run", run)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        verify(expected, tmp_path)
