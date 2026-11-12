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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kind_release_images as guard  # noqa: E402
import consumer_qualification as consumer  # noqa: E402
import qualify_kind_consumers as workflow  # noqa: E402
from container_release import write  # noqa: E402
from container_release_history import sha256  # noqa: E402
from image_release_needed import changed_inputs  # noqa: E402

IMAGE = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64
CANDIDATE = {"registryDigest": IMAGE, "imageId": CONFIG}
repo = legacy.repo


def expected(root, source):
    return consumer.inputs(root, source, legacy.PACKAGE, "shell", "docker", CANDIDATE)


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
        f"packages/{legacy.PACKAGE}/src/module.py",
        "scripts/shell_qualification.py",
        "scripts/check_container_release_image.py",
        consumer.WORKFLOW,
        ".github/actions/setup-classic-docker/action.yml",
        ".python-version",
    ],
)
def test_consumer_inputs_change_without_rebuilding_workload(repo, path):
    root, base = repo
    before = expected(root, base)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    head = legacy.commit(root)
    assert expected(root, head) != before


def test_docker_setup_change_invalidates_legacy_qualification(repo):
    root, base = repo
    action = root / ".github/actions/setup-classic-docker/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("changed\n")
    with pytest.raises(ValueError, match="consumer harness"):
        guard.matching_source(root, base, legacy.commit(root), legacy.PACKAGE, "shell")


def test_versions_and_changelog_only_reuse_the_same_subject(repo):
    root, base = repo
    before = expected(root, base)
    metadata = root / "packages" / legacy.PACKAGE / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0", "0.1.1"))
    lock = root / "uv.lock"
    lock.write_text(lock.read_text().replace("0.1.0", "0.1.1"))
    (metadata.parent / "CHANGELOG.md").write_text("release\n")
    assert expected(root, legacy.commit(root)) == before


@pytest.mark.parametrize("path", ["uv.lock", f"packages/{legacy.PACKAGE}/pyproject.toml"])
def test_real_dependency_changes_invalidate_consumer_subject(repo, path):
    root, base = repo
    target = root / path
    target.write_text(target.read_text() + '\n[qualification-test]\nrequirement = "new"\n')
    assert expected(root, legacy.commit(root)) != expected(root, base)


def test_normalization_rejects_inconsistent_workspace_version(repo):
    root, _ = repo
    lock = root / "uv.lock"
    lock.write_text(lock.read_text().replace("0.1.0", "0.1.1"))
    with pytest.raises(ValueError, match="disagree"):
        expected(root, legacy.commit(root))


@pytest.mark.parametrize(
    "path", ["images/shell-sandbox/Dockerfile", "images/shell-sandbox/runner.py"]
)
def test_image_changes_invalidate_consumer_evidence_and_remain_refresh_inputs(repo, path):
    root, base = repo
    before = expected(root, base)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new bytes\n")
    head = legacy.commit(root)
    assert expected(root, head) != before
    assert changed_inputs(root, base, head, "shell") == [path]


def test_backend_change_passes_with_fresh_consumer_evidence_and_same_image(repo, monkeypatch):
    root, base = repo
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    head = legacy.commit(root)
    record = CANDIDATE | {"sourceCommit": base}
    seen = []

    def verify(value):
        seen.append(value)
        return claim(value, head)

    monkeypatch.setattr(consumer, "verify", verify)
    result = guard.compatibility(root, head, legacy.PACKAGE, "shell", record)
    assert result[0]["sourceCommit"] == head
    assert seen == [expected(root, head)]
    assert seen[0]["registryDigest"] == IMAGE
    assert record["sourceCommit"] == base


def test_changed_image_and_backend_require_consumer_qualification(repo, monkeypatch):
    root, base = repo
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    image = root / "images/shell-sandbox/Dockerfile"
    image.parent.mkdir(parents=True)
    image.write_text("FROM scratch\n")
    head = legacy.commit(root)

    def unavailable(_):
        raise ValueError("Run consumer qualification")

    monkeypatch.setattr(consumer, "verify", unavailable)
    with pytest.raises(ValueError) as error:
        guard.compatibility(root, head, legacy.PACKAGE, "shell", CANDIDATE | {"sourceCommit": base})
    assert "maf-sandbox-docker" in str(error.value)
    assert "publish replacement image" not in str(error.value)
    assert "Run consumer qualification" in str(error.value)
    assert "Image source differences alone do not require replacement" in str(error.value)
    assert "--ref main -f profile=shell" in str(error.value)


@pytest.mark.parametrize(
    "failure",
    [
        "Matching authenticated consumer qualification unavailable",
        "Consumer attestation subject or predicate differs",
    ],
)
def test_consumer_only_failure_prescribes_same_digest_qualification(repo, monkeypatch, failure):
    root, base = repo
    (root / "packages/maf-sandbox-docker/src/module.py").write_text("VALUE = 2\n")
    head = legacy.commit(root)

    def unavailable(_):
        raise ValueError(failure)

    monkeypatch.setattr(consumer, "verify", unavailable)
    with pytest.raises(ValueError) as error:
        guard.compatibility(root, head, legacy.PACKAGE, "shell", CANDIDATE | {"sourceCommit": base})
    message = str(error.value)
    assert failure in message
    assert "shell/docker" in message
    assert "verify or refresh consumer qualification" in message
    assert f"{guard.PREFIX}/shell@{IMAGE}" in message
    assert "Image source differences alone do not require replacement" in message
    assert message.splitlines()[-1] == (
        "gh workflow run kind-consumer-qualification.yml "
        f"--repo {consumer.REPOSITORY} --ref main -f profile=shell -f source={head}"
    )
    assert "exact consumer revision" in message
    assert "publish replacement image" not in message


@pytest.mark.parametrize("path", ["Dockerfile", "runner.py", "dependencies.json"])
@pytest.mark.parametrize("evidence", ["fresh", "stale", "other-image", "failed-runtime"])
def test_changed_image_inputs_require_passing_evidence_for_current_consumer_and_image(
    repo, monkeypatch, path, evidence
):
    root, base = repo
    before = expected(root, base)
    image = root / "images/shell-sandbox" / path
    image.parent.mkdir(parents=True)
    image.write_text("changed image input\n")
    head = legacy.commit(root)
    subject = before if evidence == "stale" else expected(root, head)
    if evidence == "other-image":
        subject["registryDigest"] = "sha256:" + "f" * 64
    record = claim(subject, base if evidence == "stale" else head)
    if evidence == "failed-runtime":
        record["runtime"]["passed"] = False

    def verify(value):
        consumer.validate_record(record, value)
        return record

    monkeypatch.setattr(consumer, "verify", verify)
    candidate = CANDIDATE | {"sourceCommit": base}
    if evidence == "fresh":
        result = guard.compatibility(root, head, legacy.PACKAGE, "shell", candidate)
        assert result[0]["sourceCommit"] == head
        assert subject["registryDigest"] == IMAGE
        assert candidate["sourceCommit"] == base
    else:
        with pytest.raises(ValueError, match="mismatched identity|Consumer runtime failed"):
            guard.compatibility(root, head, legacy.PACKAGE, "shell", candidate)


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
                {
                    "priceType": p,
                    "checkedPricesIntegrity": "trusted",
                    "reportIntegrity": "untrusted",
                    "rows": 2,
                }
                for p in ("Reservation", "Consumption")
            ],
        },
    }
    consumer.validate_runtime(runtime, subject)
    for field, value in (
        ("checkedPricesIntegrity", "untrusted"),
        ("checkedPricesIntegrity", None),
        ("reportIntegrity", "trusted"),
        ("reportIntegrity", None),
    ):
        mutated = copy.deepcopy(runtime)
        mutated["azurePricing"]["checks"][2][field] = value
        with pytest.raises(ValueError, match="pricing or enforcement checks"):
            consumer.validate_runtime(mutated, subject)
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
        workflow.verified_source_commit(root)


def test_signer_recomputes_transferred_inputs_and_attempt(repo, tmp_path, monkeypatch):
    root, source = repo
    trusted(monkeypatch, root, source)
    directory = tmp_path / "transfer"
    subject = expected(root, source)
    record = claim(subject, source)
    write(directory / consumer.SUBJECT, subject)
    write(directory / "consumer-qualification.json", record)
    workflow.validate_for_signing(root, directory, legacy.PACKAGE, "shell", "docker")
    record["runAttempt"] = "2"
    write(directory / "consumer-qualification.json", record)
    with pytest.raises(ValueError, match="workflow attempt"):
        workflow.validate_for_signing(root, directory, legacy.PACKAGE, "shell", "docker")


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
    legacy.commit(root)
    assert workflow.selection(root, base, "shell") == [
        {"package": legacy.PACKAGE, "profile": "shell", "backend": "docker"}
    ]


@pytest.mark.parametrize("changed_image", [False, True])
@pytest.mark.parametrize("failure", [None, "release-identity", "runtime"])
@pytest.mark.parametrize("historical", [False, True])
def test_execution_authenticates_before_pull_and_only_emits_complete_evidence(
    repo, tmp_path, monkeypatch, failure, changed_image, historical
):
    import check_container_release_image
    import container_release_registry

    root, published_source = repo
    if changed_image:
        image = root / "images/shell-sandbox/Dockerfile"
        image.parent.mkdir(parents=True)
        image.write_text("FROM scratch\n")
        source = legacy.commit(root)
    else:
        source = published_source
    tested = source
    checkout = root
    if historical:
        (root / "pyproject.toml").write_text("# New main inputs\n")
        source = legacy.commit(root)
        checkout = tmp_path / "consumer"
        subprocess.run(
            ["git", "clone", "--quiet", str(root), str(checkout)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "checkout", "--detach", tested], cwd=checkout, check=True, capture_output=True
        )
    trusted(monkeypatch, root, source)
    candidate = CANDIDATE | {"profile": "shell"}
    record = candidate | {"sourceCommit": published_source, "candidate": candidate}
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
        return {"original": "authenticated runtime"}

    def pull(selected):
        assert selected == candidate
        calls.append("anonymous-pull")
        return CONFIG

    def runtime(profile, image_id, _, *, published_runtime):
        assert (profile, image_id) == ("shell", CONFIG)
        assert published_runtime == {"original": "authenticated runtime"}
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

    def historical_check(selected, profile, image_id, directory, published_runtime):
        assert selected == checkout
        return {
            "runtime": runtime(profile, image_id, directory, published_runtime=published_runtime),
            "python": "3.14.4",
            "distributions": [["historical-environment", "1.0"]],
        }

    monkeypatch.setattr(workflow, "historical_runtime", historical_check)
    monkeypatch.setattr(subprocess, "check_output", command)
    output = tmp_path / "evidence"
    if failure:
        with pytest.raises(ValueError):
            workflow.qualify(
                root,
                legacy.PACKAGE,
                "shell",
                "docker",
                output,
                checkout=checkout,
                requested=tested,
            )
        assert not (output / "consumer-qualification.json").exists()
        if failure == "release-identity":
            assert calls == ["authenticate"]
    else:
        workflow.qualify(
            root,
            legacy.PACKAGE,
            "shell",
            "docker",
            output,
            checkout=checkout,
            requested=tested,
        )
        assert calls == ["authenticate", "initial-qualification", "anonymous-pull", "runtime"]
        workflow.validate_for_signing(
            root, output, legacy.PACKAGE, "shell", "docker", requested=tested
        )
        assert json.loads((output / consumer.SUBJECT).read_text()) == expected(root, tested)
        result = json.loads((output / "consumer-qualification.json").read_text())
        assert result["sourceCommit"] == source
        assert result["consumerCommit"] == tested
        if historical:
            assert result["environment"]["python"] == "3.14.4"
            assert result["environment"]["distributions"] == [["historical-environment", "1.0"]]


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
        guard.compatibility(
            root, source, legacy.PACKAGE, "shell", CANDIDATE | {"sourceCommit": source}
        )


def test_shared_drawio_notice_requires_fresh_export_evidence(repo, monkeypatch):
    root, _ = repo
    package = "maf-sandbox-drawio"
    metadata = root / "packages" / package / "pyproject.toml"
    metadata.parent.mkdir()
    metadata.write_text(f'[project]\nname = "{package}"\nversion = "0.1.0"\n')
    source = metadata.parent / "src"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n")
    notice = root / "images/drawio-sandbox/DEJAVU-LICENSE"
    notice.parent.mkdir(parents=True)
    notice.write_text("original notice\n")
    base = legacy.commit(root)
    before = consumer.inputs(root, base, package, "drawio-export", "docker", CANDIDATE)
    catalogue = {
        "current": {"drawio-export": "drawio-export/0.1.0"},
        "releases": {"drawio-export/0.1.0": CANDIDATE},
    }
    monkeypatch.setattr(
        workflow,
        "History",
        lambda: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    assert workflow.selection(root, base, "drawio-export") == []
    notice.write_text("changed notice\n")
    head = legacy.commit(root)
    after = consumer.inputs(root, head, package, "drawio-export", "docker", CANDIDATE)
    assert after != before
    assert workflow.selection(root, base, "drawio-export") == [
        {"package": package, "profile": "drawio-export", "backend": "docker"}
    ]
    with pytest.raises(ValueError, match="consumer harness"):
        guard.matching_source(root, base, head, package, "drawio-export")
    monkeypatch.setattr(
        consumer, "verify", lambda value: consumer.validate_record(claim(before, base), value)
    )
    with pytest.raises(ValueError, match="mismatched identity"):
        guard.compatibility(
            root, head, package, "drawio-export", CANDIDATE | {"sourceCommit": base}
        )


@pytest.mark.parametrize("change", ["added", "removed"])
@pytest.mark.parametrize("fresh", [False, True])
def test_workspace_package_changes_require_independent_evidence(repo, monkeypatch, change, fresh):
    root, base = repo
    metadata = root / "packages/maf-sandbox-new-kind/pyproject.toml"
    metadata.parent.mkdir()
    metadata.write_text('[project]\nname = "maf-sandbox-new-kind"\nversion = "0.1.0"\n')
    if change == "removed":
        base = legacy.commit(root)
        metadata.unlink()
    head = legacy.commit(root)
    with pytest.raises(subprocess.CalledProcessError):
        guard.matching_source(root, base, head, legacy.PACKAGE, "shell")

    def verify(value):
        if not fresh:
            raise ValueError("fresh consumer evidence unavailable")
        return claim(value, head)

    monkeypatch.setattr(consumer, "verify", verify)
    record = CANDIDATE | {"sourceCommit": base}
    if fresh:
        result = guard.compatibility(root, head, legacy.PACKAGE, "shell", record)
        assert result[0]["sourceCommit"] == head
    else:
        with pytest.raises(ValueError, match="fresh consumer evidence unavailable"):
            guard.compatibility(root, head, legacy.PACKAGE, "shell", record)


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
    upload = next(
        step
        for step in jobs["qualify"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    download = next(
        step
        for step in jobs["attest"]["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert upload["with"]["overwrite"] is True
    assert upload["with"]["name"] == download["with"]["name"]
    assert not any(" qualify " in step.get("run", "") for step in jobs["attest"]["steps"])
    signer = jobs["attest"]["steps"][-1]
    assert signer["with"]["predicate-type"] == consumer.PREDICATE
    assert "push-to-registry" not in signer["with"]
