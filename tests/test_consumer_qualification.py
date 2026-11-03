"""Independent qualification preserves image identity and rejects mismatched evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_kind_release_images as legacy
import yaml
from test_kind_release_images import PACKAGE, commit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kind_release_images as guard  # noqa: E402
import consumer_qualification as consumer  # noqa: E402
import qualify_kind_consumers as workflow  # noqa: E402
from container_release import write  # noqa: E402
from container_release_history import sha256  # noqa: E402

IMAGE = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64
CANDIDATE = {"registryDigest": IMAGE, "imageId": CONFIG}
repo = legacy.repo


def expected(root, source):
    return consumer.inputs(root, source, PACKAGE, "shell", "docker", CANDIDATE)


def claim(inputs, source):
    return {
        "schemaVersion": 1,
        "inputs": inputs,
        "sourceCommit": source,
        "runId": "123",
        "runAttempt": "1",
        "checkedAt": "2026-11-03T00:00:00Z",
        "anonymousPullVerified": True,
        "environment": {
            "python": "3.13.12",
            "docker": {"Os": "linux"},
            "distributions": [["example", "1.0"]],
        },
        "runtime": {
            "passed": True,
            "profile": inputs["profile"],
            "imageId": inputs["imageId"],
            "containersRemaining": [],
        },
    }


@pytest.mark.parametrize(
    "path",
    [
        "packages/maf-sandbox/src/module.py",
        "packages/maf-sandbox-docker/src/module.py",
        f"packages/{PACKAGE}/src/module.py",
        "scripts/shell_qualification.py",
        "scripts/check_container_release_image.py",
        consumer.WORKFLOW,
        ".python-version",
    ],
)
def test_consumer_inputs_change_without_rebuilding_workload(repo, path):
    root, base = repo
    before = expected(root, base)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    head = commit(root)
    assert expected(root, head) != before
    assert consumer.image_changes(root, base, head, "shell") == []


def test_versions_and_changelog_only_reuse_the_same_subject(repo):
    root, base = repo
    before = expected(root, base)
    metadata = root / "packages" / PACKAGE / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0", "0.1.1"))
    lock = root / "uv.lock"
    lock.write_text(lock.read_text().replace("0.1.0", "0.1.1"))
    (metadata.parent / "CHANGELOG.md").write_text("release\n")
    assert expected(root, commit(root)) == before


@pytest.mark.parametrize("path", ["uv.lock", f"packages/{PACKAGE}/pyproject.toml"])
def test_real_dependency_changes_invalidate_consumer_subject(repo, path):
    root, base = repo
    target = root / path
    target.write_text(target.read_text() + '\n[qualification-test]\nrequirement = "new"\n')
    assert expected(root, commit(root)) != expected(root, base)


def test_normalization_rejects_inconsistent_workspace_version(repo):
    root, _ = repo
    lock = root / "uv.lock"
    lock.write_text(lock.read_text().replace("0.1.0", "0.1.1"))
    with pytest.raises(ValueError, match="disagree"):
        expected(root, commit(root))


@pytest.mark.parametrize(
    "path", ["images/shell-sandbox/Dockerfile", "images/shell-sandbox/runner.py"]
)
def test_image_changes_still_require_replacement(repo, path):
    root, base = repo
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new bytes\n")
    assert consumer.image_changes(root, base, commit(root), "shell") == [path]


def test_backend_change_passes_with_fresh_consumer_evidence_and_same_image(repo, monkeypatch):
    root, base = repo
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    head = commit(root)
    record = CANDIDATE | {"sourceCommit": base}
    seen = []

    def verify(value):
        seen.append(value)
        return claim(value, head)

    monkeypatch.setattr(consumer, "verify", verify)
    result = guard.compatibility(root, head, PACKAGE, "shell", record)
    assert result[0]["sourceCommit"] == head
    assert seen == [expected(root, head)]
    assert seen[0]["registryDigest"] == IMAGE
    assert record["sourceCommit"] == base


def test_all_mismatches_report_image_and_consumer_actions(repo, monkeypatch):
    root, base = repo
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    image = root / "images/shell-sandbox/Dockerfile"
    image.parent.mkdir(parents=True)
    image.write_text("FROM scratch\n")
    head = commit(root)

    def unavailable(_):
        raise ValueError("Run consumer qualification")

    monkeypatch.setattr(consumer, "verify", unavailable)
    with pytest.raises(ValueError) as error:
        guard.compatibility(root, head, PACKAGE, "shell", CANDIDATE | {"sourceCommit": base})
    assert "maf-sandbox-docker" in str(error.value)
    assert "publish replacement image" in str(error.value)
    assert "Run consumer qualification" in str(error.value)


def test_fresh_consumer_evidence_cannot_waive_image_change(repo, monkeypatch):
    root, base = repo
    image = root / "images/shell-sandbox/Dockerfile"
    image.parent.mkdir(parents=True)
    image.write_text("FROM scratch\n")
    head = commit(root)
    monkeypatch.setattr(consumer, "verify", lambda value: claim(value, head))
    with pytest.raises(ValueError, match="publish replacement image"):
        guard.compatibility(root, head, PACKAGE, "shell", CANDIDATE | {"sourceCommit": base})


@pytest.mark.parametrize(
    "field,value",
    [
        ("anonymousPullVerified", False),
        ("sourceCommit", "main"),
        ("runId", "0"),
        ("inputs", {}),
        ("environment", {}),
    ],
)
def test_incomplete_claims_refused(repo, field, value):
    root, source = repo
    subject = expected(root, source)
    record = claim(subject, source)
    record[field] = value
    with pytest.raises(ValueError):
        consumer.validate_record(record, subject)


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", False),
        ("passed", 1),
        ("imageId", IMAGE),
        ("profile", "other"),
        ("containersRemaining", ["leftover"]),
        ("detail", {"networksRemaining": ["leftover"]}),
    ],
)
def test_runtime_mismatch_or_cleanup_failure_refused(repo, field, value):
    root, source = repo
    subject = expected(root, source)
    record = claim(subject, source)
    record["runtime"][field] = value
    with pytest.raises(ValueError):
        consumer.validate_record(record, subject)


def test_pricing_requires_proxy_identity_and_enforcement_checks():
    subject = CANDIDATE | {"profile": "azure-pricing"}
    runtime = {
        "passed": True,
        "profile": "azure-pricing",
        "imageId": CONFIG,
        "containersRemaining": [],
        "azurePricing": {
            "backend": "docker",
            "imageId": CONFIG,
            "proxyImageId": IMAGE,
            "livePricingVerified": True,
            "containersRemaining": [],
            "networksRemaining": [],
            "checks": [{"probe": p, "denied": True} for p in ("unlisted-host", "post-method")]
            + [
                {"priceType": p, "integrity": "trusted", "rows": 2}
                for p in ("Reservation", "Consumption")
            ],
        },
    }
    consumer.validate_runtime(runtime, subject)
    for field, value in (("proxyImageId", "latest"), ("checks", []), ("networksRemaining", ["n"])):
        mutated = copy.deepcopy(runtime)
        mutated["azurePricing"][field] = value
        with pytest.raises(ValueError):
            consumer.validate_runtime(mutated, subject)


def attestation(subject, record):
    return [
        {
            "verificationResult": {
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": consumer.PREDICATE,
                    "subject": [
                        {
                            "name": consumer.SUBJECT,
                            "digest": {
                                "sha256": sha256(subject.read_bytes()).removeprefix("sha256:")
                            },
                        }
                    ],
                    "predicate": record,
                }
            }
        }
    ]


@pytest.mark.parametrize("failure", [None, "subject", "predicate", "signature", "source", "stale"])
def test_verification_authenticates_subject_workflow_and_checkout(repo, monkeypatch, failure):
    root, source = repo
    subject = expected(root, source)
    record = claim(subject, source)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[command.index("--cert-identity") + 1] == consumer.SIGNER
        assert command[command.index("--cert-oidc-issuer") + 1] == consumer.ISSUER
        assert command[command.index("--source-ref") + 1] == "refs/heads/main"
        assert "--deny-self-hosted-runners" in command
        if failure == "signature" or (failure == "source" and "--source-digest" in command):
            raise subprocess.CalledProcessError(1, command)
        values = attestation(Path(command[3]), copy.deepcopy(record))
        statement = values[0]["verificationResult"]["statement"]
        if failure == "subject":
            statement["subject"][0]["digest"]["sha256"] = "f" * 64
        elif failure == "predicate":
            statement["predicateType"] = "other"
        elif failure == "stale":
            statement["predicate"]["inputs"]["registryDigest"] = "sha256:" + "f" * 64
        return subprocess.CompletedProcess(command, 0, json.dumps(values))

    monkeypatch.setattr(subprocess, "run", run)
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            consumer.verify(subject)
    else:
        assert consumer.verify(subject) == record
        assert commands[1][-4:] == ["--source-digest", source, "--signer-digest", source]


def trusted(monkeypatch, root, source):
    for key, value in {
        "GITHUB_REPOSITORY": consumer.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": source,
        "GITHUB_WORKFLOW_REF": f"{consumer.REPOSITORY}/{consumer.WORKFLOW}@refs/heads/main",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REF", "refs/pull/1/merge"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_SHA", "a" * 40),
        ("GITHUB_WORKFLOW_REF", "another-workflow"),
    ],
)
def test_untrusted_context_cannot_produce_evidence(repo, monkeypatch, key, value):
    root, source = repo
    trusted(monkeypatch, root, source)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="trusted main"):
        workflow.trusted_source(root)


def test_signer_recomputes_transferred_inputs_and_attempt(repo, tmp_path, monkeypatch):
    root, source = repo
    trusted(monkeypatch, root, source)
    directory = tmp_path / "transfer"
    subject = expected(root, source)
    record = claim(subject, source)
    write(directory / consumer.SUBJECT, subject)
    write(directory / "consumer-qualification.json", record)
    workflow.validate_for_signing(root, directory, PACKAGE, "shell", "docker")
    record["runAttempt"] = "2"
    write(directory / "consumer-qualification.json", record)
    with pytest.raises(ValueError, match="workflow attempt"):
        workflow.validate_for_signing(root, directory, PACKAGE, "shell", "docker")


def test_selection_ignores_metadata_but_selects_backend_changes(repo, monkeypatch):
    root, base = repo
    catalogue = {"current": {"shell": "shell/0.1.0"}, "releases": {"shell/0.1.0": CANDIDATE}}
    monkeypatch.setattr(
        workflow,
        "History",
        lambda: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    assert workflow.selection(root, base, "shell") == []
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    commit(root)
    assert workflow.selection(root, base, "shell") == [
        {"package": PACKAGE, "profile": "shell", "backend": "docker"}
    ]


@pytest.mark.parametrize("failure", [None, "release-identity", "runtime"])
def test_execution_authenticates_before_pull_and_only_emits_complete_evidence(
    repo, tmp_path, monkeypatch, failure
):
    import check_container_release_image
    import container_release_registry

    root, source = repo
    trusted(monkeypatch, root, source)
    candidate = CANDIDATE | {"profile": "shell"}
    record = candidate | {"sourceCommit": source, "candidate": candidate}
    catalogue = {"current": {"shell": "shell/0.1.0"}, "releases": {"shell/0.1.0": record}}
    monkeypatch.setattr(
        workflow,
        "History",
        lambda _: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    calls = []

    def authenticate(*_):
        calls.append("authenticate")
        if failure == "release-identity":
            raise ValueError("Invalid release identity")
        return "https://example.com/release"

    def initial(*_):
        calls.append("initial-qualification")

    def pull(selected):
        assert selected == candidate
        calls.append("anonymous-pull")
        return CONFIG

    def runtime(profile, image_id, _):
        assert (profile, image_id) == ("shell", CONFIG)
        calls.append("runtime")
        return {
            "passed": failure != "runtime",
            "profile": profile,
            "imageId": image_id,
            "containersRemaining": [],
        }

    original = subprocess.check_output

    def command(args, **kwargs):
        if args[0] == "docker":
            assert args == ["docker", "version", "--format", "{{json .Server}}"]
            return '{"Os": "linux", "Arch": "amd64"}'
        return original(args, **kwargs)

    monkeypatch.setattr(workflow, "release_evidence", authenticate)
    monkeypatch.setattr(guard, "qualified", initial)
    monkeypatch.setattr(container_release_registry, "anonymous_pull", pull)
    monkeypatch.setattr(check_container_release_image, "check", runtime)
    monkeypatch.setattr(subprocess, "check_output", command)
    output = tmp_path / "evidence"
    if failure:
        with pytest.raises(ValueError):
            workflow.qualify(root, PACKAGE, "shell", "docker", output)
        assert not (output / "consumer-qualification.json").exists()
        if failure == "release-identity":
            assert calls == ["authenticate"]
    else:
        workflow.qualify(root, PACKAGE, "shell", "docker", output)
        assert calls == ["authenticate", "initial-qualification", "anonymous-pull", "runtime"]
        workflow.validate_for_signing(root, output, PACKAGE, "shell", "docker")
        assert json.loads((output / consumer.SUBJECT).read_text()) == expected(root, source)


def test_legacy_docker_evidence_cannot_cover_an_additional_backend(repo, monkeypatch):
    root, source = repo
    monkeypatch.setitem(consumer.BACKENDS, "shell", ("docker", "wslc"))
    monkeypatch.setattr(consumer, "inputs", lambda r, h, p, f, b, c: {"backend": b})

    def verify(value):
        if value["backend"] == "wslc":
            raise ValueError("WSLC evidence unavailable")
        return {"sourceCommit": source, "runId": "123"}

    monkeypatch.setattr(consumer, "verify", verify)
    with pytest.raises(ValueError, match="WSLC evidence unavailable"):
        guard.compatibility(root, source, PACKAGE, "shell", CANDIDATE | {"sourceCommit": source})


@pytest.mark.workflow
def test_workflow_separates_execution_from_signing_and_never_publishes_images():
    root = Path(__file__).resolve().parents[1]
    value = yaml.safe_load((root / consumer.WORKFLOW).read_text("utf-8"))
    events = value.get("on", value.get(True))
    assert set(events) == {"push", "workflow_run", "workflow_dispatch"}
    assert events["push"]["branches"] == ["main"]
    assert "id-token" not in value["permissions"]
    jobs = value["jobs"]
    assert "github.ref == 'refs/heads/main'" in jobs["select"]["if"]
    for name, job in jobs.items():
        permissions = job.get("permissions", value["permissions"])
        assert permissions.get("contents") == "read"
        assert "packages" not in permissions
        if name != "attest":
            assert "id-token" not in permissions
            assert permissions["attestations"] == "read"
    assert jobs["attest"]["permissions"]["id-token"] == "write"
    assert jobs["attest"]["needs"] == ["select", "qualify"]
    assert not any(" qualify " in step.get("run", "") for step in jobs["attest"]["steps"])
    signer = jobs["attest"]["steps"][-1]
    assert signer["with"]["predicate-type"] == consumer.PREDICATE
    assert "push-to-registry" not in signer["with"]
