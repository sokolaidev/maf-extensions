"""Published runtime verification refuses untrusted provenance before executing image code."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_hyperlight_aks_image as builder
import verify_hyperlight_aks_image as verifier

DIGEST = "1" * 64
IMAGE = f"registry.example/hyperlight@sha256:{DIGEST}"
IMAGE_ID = "sha256:" + "2" * 64
REVISION = "a" * 40
INPUTS = "b" * 64
SIGNER = "https://github.com/example/builders/.github/workflows/runtime.yml@refs/heads/main"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    commands = []
    attestation = {
        "attestation": {"bundle": {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}},
        "verificationResult": {
            "statement": {
                "predicateType": verifier.PREDICATE,
                "subject": [{"name": "registry.example/hyperlight", "digest": {"sha256": DIGEST}}],
            },
        },
    }
    source = {"repository": verifier.SOURCE_URL, "revision": REVISION, "dirty": False}
    smoke = {
        "build_inputs_sha256": INPUTS,
        "source": source,
        "hypervisor_execution_verified": False,
    }
    state = {"attestations": [attestation], "smoke": smoke, "fail": None}
    output = tmp_path / "provenance.json"
    output.write_text("previous success")

    def execute(command, **kwargs):
        commands.append(command)
        assert not output.exists()
        assert kwargs["check"] is True
        if state["fail"] == command[0:2]:
            raise subprocess.CalledProcessError(1, command)
        if command[0] == "gh":
            assert commands == [command]
            return subprocess.CompletedProcess(command, 0, json.dumps(state["attestations"]))
        if command[1] == "pull":
            assert command[-1] == IMAGE
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "image":
            if "--format" in command:
                assert command[-1] == IMAGE
                return subprocess.CompletedProcess(command, 0, IMAGE_ID + "\n")
            assert command[-1] == IMAGE_ID
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Os": "linux",
                            "Architecture": "amd64",
                            "Config": {
                                "User": "65534:65534",
                                "Cmd": builder.PROBE_COMMAND,
                                "Entrypoint": None,
                            },
                        }
                    ]
                ),
            )
        if command[1] in {"create", "rm"}:
            assert kwargs["timeout"] == builder.DOCKER_TIMEOUT
            return subprocess.CompletedProcess(command, 0)
        assert command[1] == "start"
        return subprocess.CompletedProcess(command, 0, json.dumps(state["smoke"]))

    async def capture(command):
        return execute(command, check=True).stdout.encode()

    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(builder, "_smoke_output", capture)
    options = {
        "signer_identity": SIGNER,
        "source_revision": REVISION,
        "source_ref": "refs/heads/main",
        "build_inputs_sha256": INPUTS,
        "output": output,
    }
    return state, commands, options, output


def test_verified_digest_and_payload_record_preserves_proof_and_policy(scenario):
    state, commands, options, output = scenario
    report = verifier.verify_published_image(IMAGE, **options)
    assert json.loads(output.read_text()) == report
    assert report["signed_provenance_verified"] is True
    assert report["registry_digest"] == "sha256:" + DIGEST
    assert report["local_image_id"] == IMAGE_ID
    assert report["attestations"] == state["attestations"]
    smoke = report["smoke"]
    assert isinstance(smoke, dict)
    assert smoke["hypervisor_execution_verified"] is False
    command = commands[0]
    assert command[:4] == ["gh", "attestation", "verify", "oci://" + IMAGE]
    for flag, expected in {
        "--hostname": "github.com",
        "--repo": verifier.REPOSITORY,
        "--cert-identity": SIGNER,
        "--cert-oidc-issuer": verifier.ISSUER,
        "--source-digest": REVISION,
        "--source-ref": "refs/heads/main",
        "--predicate-type": verifier.PREDICATE,
        "--format": "json",
    }.items():
        assert command[command.index(flag) + 1] == expected
    assert "--deny-self-hosted-runners" in command
    assert not {"--signer-workflow", "--signer-repo", "--cert-identity-regex"}.intersection(command)
    assert commands[1] == ["docker", "pull", "--platform", "linux/amd64", IMAGE]
    smoke_command = next(command for command in commands if command[1] == "create")
    assert smoke_command[-4:] == [IMAGE_ID, "-I", "-B", "/opt/verify.py"]
    name = smoke_command[smoke_command.index("--name") + 1]
    assert commands[-2:] == [
        ["docker", "start", "--attach", name],
        ["docker", "rm", "--force", name],
    ]
    assert "--pull=never" in smoke_command
    assert "--read-only" in smoke_command
    assert smoke_command[smoke_command.index("--network") + 1] == "none"
    assert "--privileged" not in smoke_command
    assert "--device" not in smoke_command


@pytest.mark.parametrize(
    "step",
    [
        ["gh", "attestation"],
        ["docker", "pull"],
        ["docker", "image"],
        ["docker", "create"],
        ["docker", "start"],
        ["docker", "rm"],
    ],
)
def test_failure_never_retains_success_or_runs_subsequent_steps(scenario, step):
    state, commands, options, output = scenario
    state["fail"] = step
    with pytest.raises(subprocess.CalledProcessError):
        verifier.verify_published_image(IMAGE, **options)
    assert commands[-1][:2] == (["docker", "rm"] if step[1] in {"create", "start"} else step)
    assert not output.exists()
    if step[0] == "gh":
        assert len(commands) == 1


@pytest.mark.parametrize("attestations", [[], {}, None, [None], [{}], [{"verificationResult": {}}]])
def test_empty_or_malformed_verified_results_refuse_before_docker(scenario, attestations):
    state, commands, options, output = scenario
    state["attestations"] = attestations
    with pytest.raises(ValueError):
        verifier.verify_published_image(IMAGE, **options)
    assert len(commands) == 1
    assert not output.exists()


@pytest.mark.parametrize("change", ["digest", "predicate"])
def test_unexpected_statement_refuses_before_docker(scenario, change):
    state, commands, options, output = scenario
    statement = state["attestations"][0]["verificationResult"]["statement"]
    if change == "digest":
        statement["subject"][0]["digest"]["sha256"] = "c" * 64
    else:
        statement["predicateType"] = "https://example.com/other"
    with pytest.raises(ValueError, match="requested image"):
        verifier.verify_published_image(IMAGE, **options)
    assert len(commands) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dirty", True),
        ("dirty", None),
        ("dirty", 0),
        ("revision", "c" * 40),
        ("repository", "https://github.com/example/fork"),
    ],
)
def test_authenticated_image_still_requires_matching_clean_payload(scenario, field, value):
    state, _, options, output = scenario
    state["smoke"]["source"][field] = value
    with pytest.raises(ValueError, match="clean source"):
        verifier.verify_published_image(IMAGE, **options)
    assert not output.exists()


def test_authenticated_image_still_requires_expected_build_inputs(scenario):
    state, _, options, output = scenario
    state["smoke"]["build_inputs_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="build inputs"):
        verifier.verify_published_image(IMAGE, **options)
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image", "registry.example/hyperlight:latest"),
        ("image", "registry.example/hyperlight:tag@sha256:" + DIGEST),
        ("image", "--help"),
        ("image", "namespace/hyperlight@sha256:" + DIGEST),
        ("image", "https://registry.example/hyperlight@sha256:" + DIGEST),
        ("image", "user:password@registry.example/hyperlight@sha256:" + DIGEST),
        ("source_revision", "main"),
        ("source_revision", "a" * 7),
        ("source_ref", "main"),
        ("source_ref", "refs/pull/1/merge"),
        ("signer_identity", "https://github.com/example/builders"),
        ("signer_identity", SIGNER.replace("github.com", "example.com")),
        ("build_inputs_sha256", "unknown"),
    ],
)
def test_invalid_operator_policy_refuses_before_external_commands(scenario, field, value):
    _, commands, options, output = scenario
    image = IMAGE
    if field == "image":
        image = value
    else:
        options[field] = value
    with pytest.raises(ValueError):
        verifier.verify_published_image(image, **options)
    assert commands == []
    assert not output.exists()


def test_failed_record_replacement_leaves_no_success_or_temporary_file(scenario, monkeypatch):
    _, _, options, output = scenario

    def refuse(*args):
        raise OSError("cannot replace record")

    monkeypatch.setattr(verifier.os, "replace", refuse)
    with pytest.raises(OSError, match="cannot replace"):
        verifier.verify_published_image(IMAGE, **options)
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("bundle", [None, {}, "unverified"])
def test_success_without_retained_signed_bundle_is_refused(scenario, bundle):
    state, commands, options, output = scenario
    state["attestations"][0]["attestation"]["bundle"] = bundle
    with pytest.raises(ValueError, match="invalid verification result"):
        verifier.verify_published_image(IMAGE, **options)
    assert len(commands) == 1
    assert not output.exists()


@pytest.mark.parametrize("error", [TimeoutError("deadline"), ValueError("output limit")])
def test_smoke_refusal_removes_container_and_success_record(scenario, monkeypatch, error):
    _, commands, options, output = scenario

    async def refuse(command):
        raise error

    monkeypatch.setattr(builder, "_smoke_output", refuse)
    with pytest.raises(type(error), match=str(error)):
        verifier.verify_published_image(IMAGE, **options)
    create = commands[-2]
    name = create[create.index("--name") + 1]
    assert commands[-1] == ["docker", "rm", "--force", name]
    assert not output.exists()


def test_cli_preserves_attestation_failure_diagnostics(monkeypatch, tmp_path, capsys):
    error = subprocess.CalledProcessError(1, ["gh"], output="Sigstore verification failed")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(verifier, "verify_published_image", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_hyperlight_aks_image.py",
            "--image",
            IMAGE,
            "--signer-identity",
            SIGNER,
            "--source-revision",
            REVISION,
            "--source-ref",
            "refs/heads/main",
            "--build-inputs-sha256",
            INPUTS,
            "--output",
            str(tmp_path / "evidence.json"),
        ],
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        verifier.main()
    assert caught.value is error
    assert "Sigstore verification failed" in capsys.readouterr().err
