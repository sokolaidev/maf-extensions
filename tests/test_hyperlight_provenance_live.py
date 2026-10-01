"""The live provenance harness must distinguish expected refusals from broken infrastructure."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_hyperlight_provenance_live as live

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.workflow


@pytest.mark.parametrize(
    ("status", "stderr", "stale"),
    [
        (0, "verification failed", False),
        (1, "registry connection timed out", False),
        (1, "verification failed", True),
    ],
)
def test_refusal_requires_failure_reason_and_no_stale_record(tmp_path, status, stderr, stale):
    output = tmp_path / "record.json"
    if stale:
        output.write_text('{"signed_provenance_verified": true}')
    with pytest.raises(ValueError, match="expected reason"):
        live.check_result(
            subprocess.CompletedProcess([], status, "", stderr), output, "verification failed"
        )


@pytest.mark.parametrize("contents", [None, "{}", '{"signed_provenance_verified": false}'])
def test_positive_requires_retained_verified_record(tmp_path, contents):
    output = tmp_path / "record.json"
    if contents is not None:
        output.write_text(contents)
    with pytest.raises(ValueError):
        live.check_result(subprocess.CompletedProcess([], 0, "", ""), output, None)


def test_real_cli_matrix_retains_only_registry_independent_evidence(tmp_path, monkeypatch):
    seen = []
    registry = "registry.example"
    signed = f"{registry}/runtime@sha256:" + "a" * 64
    unsigned = f"{registry}/runtime@sha256:" + "b" * 64
    signer = (
        "https://github.com/sokolaidev/maf-extensions/.github/workflows/live.yml@refs/heads/main"
    )

    def run(command, **kwargs):
        options = dict(zip(command[2::2], command[3::2], strict=True))
        output = Path(options["--output"])
        if options["--image"] == unsigned:
            error = "no attestations"
        elif options["--build-inputs-sha256"] != "d" * 64:
            error = "image build inputs do not match"
        elif options["--signer-identity"] != signer:
            error = 'Error: verifying with issuer "sigstore.dev"'
        elif options["--source-revision"] != "c" * 40:
            error = "expected SourceRepositoryDigest to be"
        elif options["--source-ref"] != "refs/heads/main":
            error = "expected SourceRepositoryRef to be"
        else:
            error = ""
        seen.append((options, error))
        if error:
            assert json.loads(output.read_text())["signed_provenance_verified"] is True
            output.unlink()
            live.sidecar_path(output).unlink(missing_ok=True)
        else:
            assert not output.exists()
            live.sidecar_path(output).write_bytes(b"original proof bytes")
            output.write_text(
                json.dumps(
                    {
                        "image": signed,
                        "signed_provenance_verified": True,
                        "retained_evidence": {
                            "archive": live.sidecar_path(output).name,
                            "archive_sha256": live.sha(live.sidecar_path(output)),
                            "offline_verified": True,
                        },
                    }
                )
            )
        return subprocess.CompletedProcess(command, bool(error), "", error)

    monkeypatch.setattr(live.subprocess, "run", run)
    live.exercise(signed, unsigned, signer, "c" * 40, "refs/heads/main", "d" * 64, tmp_path)
    assert len(seen) == 7
    report = (tmp_path / "verification.json").read_text()
    assert registry not in report
    assert all(case["passed"] for case in json.loads(report)["integration_cases"])


def test_workflow_separates_signing_verification_and_cleanup():
    workflow = yaml.safe_load((ROOT / ".github/workflows/hyperlight-provenance.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"]["attestations"] == "write"
    assert jobs["verify"]["permissions"]["attestations"] == "read"
    assert jobs["verify"]["needs"] == "build"
    assert "id-token" not in jobs["verify"]["permissions"]
    for job in jobs.values():
        assert job["steps"][-1]["if"] == "always()"
    assert all(job["runs-on"] == "ubuntu-latest" for job in jobs.values())
    assert all("environment" not in job for job in jobs.values())
    assert "secrets." not in yaml.safe_dump(workflow)
    caller = yaml.safe_load((ROOT / ".github/workflows/workflow-tests.yml").read_text())["jobs"][
        "hyperlight-provenance"
    ]
    assert (
        caller["if"] == "github.event_name == 'workflow_dispatch' && inputs.hyperlight_provenance"
    )
    assert "secrets" not in caller
