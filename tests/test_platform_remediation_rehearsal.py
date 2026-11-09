"""Manual rehearsals retain production evidence guards and isolate every App write."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_remediation_evidence as acquisition  # noqa: E402
import image_remediation_pr as publication  # noqa: E402
import platform_rehearsal_history as history  # noqa: E402
import platform_remediation_rehearsal as rehearsal  # noqa: E402
from container_release import REPOSITORY, digest, read, scan_result, write  # noqa: E402
from test_go_remediation import AT, IMAGE, retained  # noqa: E402
from test_image_remediation import BOT, SOURCE, URL, FakeGitHub  # noqa: E402
from test_image_remediation import app_key as app_key  # noqa: E402
from test_platform_remediation import apk, dotnet, planner  # noqa: E402
from test_platform_remediation import checkout as checkout  # noqa: E402
from test_platform_remediation import qualified as qualified  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def context(checkout, monkeypatch):
    path = checkout / rehearsal.MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / rehearsal.MANIFEST, path)
    monkeypatch.setattr(rehearsal, "git", lambda *_: SOURCE)
    for name, value in {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SOURCE,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{rehearsal.WORKFLOW}@refs/heads/main",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "REHEARSAL_CASE": "bicep-update",
    }.items():
        monkeypatch.setenv(name, value)
    return checkout


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITHUB_REPOSITORY", "another/repository"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/topic"),
        ("GITHUB_SHA", "d" * 40),
        ("GITHUB_WORKFLOW_REF", f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/main"),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ATTEMPT", "-1"),
        ("REHEARSAL_CASE", "invent-a-fix"),
    ],
)
def test_request_refuses_untrusted_context(context, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        rehearsal.request_record(context)


@pytest.mark.parametrize("case", rehearsal.CASES)
def test_case_selection_requires_real_matching_planner_rows(context, tmp_path, case):
    match, profile = dotnet(), "bicep"
    if case in {"current-rebuild", history.CASE}:
        match["vulnerability"]["fix"]["versions"] = ["3.1.12"]
    elif case == "apk-pinned":
        match, profile = apk("clamav-scanner", "1.4.6-r0", "1.4.6-r1", "alpine"), "antivirus"
    elif case == "apk-unpinned":
        match, profile = apk(), "graphviz"
    expected = planner.plan(context, [retained(tmp_path, [match], profile=profile)])
    rows = rehearsal.case_rows(context, expected, case)
    assert len(rows) == 1 and rows[0]["component"] == match["artifact"]["name"]
    expected["findings"] = []
    with pytest.raises(ValueError, match="unavailable in selected evidence"):
        rehearsal.case_rows(context, expected, case)


def test_rebuild_case_does_not_hide_unrelated_source_edits(context):
    expected = {
        "findings": [{"type": "apk", "status": "rebuild-required"}],
        "updates": [{"path": "another-file"}],
    }
    with pytest.raises(ValueError, match="without source edits"):
        rehearsal.case_rows(context, expected, "apk-unpinned")


@pytest.mark.parametrize("outcome", ["ready", "recovered", "deferred"])
def test_acquisition_reuses_authenticated_platform_scope(context, tmp_path, monkeypatch, outcome):
    work = tmp_path / "work"
    expected = {"findings": [{"type": "dotnet", "status": "update"}], "updates": [1]}
    calls = []

    def acquire(client, root, directory, **kwargs):
        calls.append((root, directory, kwargs))
        if outcome == "deferred" or (outcome == "recovered" and len(calls) == 1):
            raise acquisition.EvidenceChanging("Monitoring changed during acquisition")
        write(directory / "receipt.json", {"source": SOURCE})
        write(directory / "plan.json", expected)
        return expected

    monkeypatch.setattr(acquisition, "acquire", acquire)
    monkeypatch.setattr(acquisition.time, "sleep", lambda *_: None)
    if outcome == "deferred":
        with pytest.raises(ValueError, match="deferred"):
            rehearsal.acquire(context, work)
        assert not (work / "receipt.json").exists()
        assert not (work / "plan.json").exists()
    else:
        assert rehearsal.acquire(context, work) == expected
        assert read(work / "receipt.json") == {"source": SOURCE}
        assert read(work / "plan.json") == expected
    attempts = {"ready": 1, "recovered": 2, "deferred": 3}[outcome]
    assert calls == [
        (
            context,
            work / "attempts" / str(attempt),
            {
                "selected_profiles": tuple(rehearsal.RUNTIME_RECIPES),
                "planner": rehearsal.plan,
                "require_all_profiles": False,
            },
        )
        for attempt in range(1, attempts + 1)
    ]
    assert read(work / "rehearsal-request.json") == rehearsal.request_record(context)


def test_acquisition_refuses_existing_work_without_replacing_its_request(context, tmp_path):
    work = tmp_path / "work"
    previous = {"runId": 99}
    write(work / "rehearsal-request.json", previous)
    with pytest.raises(FileExistsError):
        rehearsal.acquire(context, work)
    assert read(work / "rehearsal-request.json") == previous


def test_bicep_qualification_revalidates_real_aggregate(qualified, context, monkeypatch):
    root, work, results = qualified
    write(work / "rehearsal-request.json", rehearsal.request_record(root))
    observed = []
    monkeypatch.setattr(rehearsal, "current", lambda client, receipt: observed.append(receipt))
    report = rehearsal.qualify(root, work, results)
    assert report["caseMatches"] == {"baseline": 3, "candidate": 0}
    assert report["qualification"]["status"] == "ready"
    assert report["productionRecovery"] is False
    assert len(observed) == 1


@pytest.mark.parametrize("field", ["source", "runId", "runAttempt", "case", "manifestSha256"])
def test_foreign_case_receipt_is_rejected(qualified, context, field):
    root, work, results = qualified
    request = rehearsal.request_record(root)
    request[field] = "foreign"
    write(work / "rehearsal-request.json", request)
    with pytest.raises(ValueError, match="another source, run, attempt or case"):
        rehearsal.qualify(root, work, results)


def test_requested_updates_cannot_pass_from_already_fixed_baselines(qualified, context):
    root, work, results = qualified
    write(work / "rehearsal-request.json", rehearsal.request_record(root))
    for directory in results.glob("baseline-*"):
        report = read(directory / "grype.json")
        report["matches"] = []
        write(directory / "grype.json", report)
        write(
            directory / "assessment.json",
            scan_result(report, IMAGE, AT, sbom=read(directory / "sbom.syft.json")),
        )
    with pytest.raises(ValueError, match="requested rehearsal case"):
        rehearsal.qualify(root, work, results)


def test_changed_current_evidence_never_admits_rehearsal(qualified, context, monkeypatch):
    root, work, results = qualified
    write(work / "rehearsal-request.json", rehearsal.request_record(root))

    def stale(*args):
        raise acquisition.EvidenceChanging("Main changed during remediation")

    monkeypatch.setattr(rehearsal, "current", stale)
    with pytest.raises(acquisition.EvidenceChanging, match="Main changed"):
        rehearsal.qualify(root, work, results)


class FakeRehearsal(rehearsal.RehearsalGitHub, FakeGitHub):
    def __init__(self):
        FakeGitHub.__init__(self)
        rehearsal.RehearsalGitHub.__init__(self, 123)


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_REMEDIATION_TOKEN", "fixture-token")
    monkeypatch.setattr(publication, "current", lambda *_: None)
    monkeypatch.setattr(rehearsal, "git", lambda *_: "")
    root = tmp_path / "root"
    write(root / publication.REHEARSAL_PATH, {"status": "not-run"})
    qualified = {
        "source": SOURCE,
        "runId": 123,
        "runAttempt": 1,
        "case": "current-rebuild",
        "qualification": {"scans": [], "files": {planner.NUGET: "a" * 64}},
    }
    report, files = rehearsal.delivery_payload(root, qualified, URL)
    return FakeRehearsal(), report, files, tmp_path / "delivery"


def test_signed_orphan_recovery_and_refresh_only_write_draft_receipt(delivery):
    client, report, files, work = delivery
    result = rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    assert result["status"] == "delivery-passed" and result["ci"] == "not-verified"
    assert result["interruption"]["head"] == client.head
    assert result["interruption"]["identity"] == {"author": BOT, "committer": BOT}
    assert {item["mode"] for item in result["ownershipRefusals"]} == {"unsigned", "altered"}
    assert all(item["scope"] == "injected-read" for item in result["ownershipRefusals"])
    assert len(client.pulls) == 1 and client.pulls[0]["draft"] is True
    assert "do not merge" in client.pulls[0]["body"]
    assert "not a proposed dependency fix" in client.pulls[0]["body"]
    assert set(files) == {publication.REHEARSAL_PATH}
    for path, data in client.writes:
        if path == "git/trees":
            assert {item["path"] for item in data["tree"]} == {publication.REHEARSAL_PATH}
        if path == "graphql":
            update = data["variables"]["input"]["refUpdates"][0]
            assert update["name"] == "refs/heads/bot/platform-remediation-rehearsal-123"
            assert update["force"] is False
    assert read(work / "rehearsal-delivery.json") == result
    commit = result["interruption"]["signedCommit"]
    assert commit["tree"]["sha"] == client.commits[client.head]["tree"]["sha"]
    assert publication.COMMIT_MARKER in commit["message"]
    repeated = rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    assert repeated["status"] == "refresh-passed"
    assert "interruption" not in repeated


@pytest.mark.parametrize("mutation", ["branch", "file", "kind", "title"])
def test_rehearsal_cannot_escape_its_destination(delivery, mutation):
    client, report, files, _ = delivery
    if mutation == "branch":
        client.branch = "bot/platform-image-remediation"
    elif mutation == "file":
        files = {planner.NUGET: next(iter(files.values()))}
        report["files"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    elif mutation == "kind":
        report["kind"] = "platform"
    else:
        client.title = publication.TITLE
    with pytest.raises(ValueError, match="isolated branch and receipt"):
        publication.publish(client, {"source": SOURCE}, report, files, BOT, URL)
    assert not client.writes


@pytest.mark.parametrize("mutation", ["draft", "title", "body", "head", "closed"])
def test_rehearsal_respects_human_changes(delivery, mutation):
    client, report, files, work = delivery
    rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    if mutation == "draft":
        client.pulls[0]["draft"] = False
    elif mutation == "title":
        client.pulls[0]["title"] = "Human title"
    elif mutation == "body":
        client.pulls[0]["body"] += "Human note"
    elif mutation == "head":
        client.head = "human"
    else:
        client.pulls[0]["state"] = "closed"
    writes = copy.deepcopy(client.writes)
    with pytest.raises(ValueError):
        rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    assert client.writes == writes


@pytest.mark.parametrize("field,value", [("draft", False), ("title", "Human title")])
def test_draft_change_between_read_and_write_is_refused(delivery, field, value):
    client, report, files, work = delivery
    rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    pull = copy.deepcopy(client.pulls[0])
    client.pulls[0][field] = value
    with pytest.raises(ValueError, match="no longer a draft"):
        publication.require_pull(client, pull)


@pytest.mark.parametrize("mutation", ["opt-in", "qualified"])
def test_delivery_cli_refuses_missing_consent_or_changed_qualification(
    tmp_path, monkeypatch, mutation
):
    request = {"source": SOURCE, "runId": 123, "runAttempt": 1, "case": "bicep-update"}
    qualified = {**request, "status": "qualified"}
    write(tmp_path / "rehearsal-qualified.json", qualified)
    monkeypatch.setattr(rehearsal, "request_record", lambda *_: request)
    monkeypatch.setattr(rehearsal, "qualify", lambda *_: qualified)
    monkeypatch.setenv("REHEARSAL_DELIVERY", "true")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rehearsal",
            "deliver",
            "--work",
            str(tmp_path),
            "--results",
            str(tmp_path),
            "--bot",
            BOT,
            "--artifact-id",
            "456",
        ],
    )
    if mutation == "opt-in":
        monkeypatch.setenv("REHEARSAL_DELIVERY", "false")
    else:
        write(tmp_path / "rehearsal-qualified.json", {**qualified, "source": "foreign"})
    calls = []
    monkeypatch.setattr(rehearsal, "delivery_payload", lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        rehearsal.main()
    assert calls == []
    failure = read(tmp_path / "rehearsal-failure.json")
    assert failure["status"] == "failed" and failure["source"] == SOURCE


def test_freshness_change_after_branch_write_preserves_a_recoverable_orphan(delivery, monkeypatch):
    client, report, files, work = delivery

    def current(*_):
        if client.head is not None:
            raise acquisition.EvidenceChanging("Monitor changed after branch write")

    monkeypatch.setattr(publication, "current", current)
    with pytest.raises(acquisition.EvidenceChanging, match="Monitor changed"):
        rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    assert client.head is not None and not client.pulls
    assert publication.orphan(client, client.head, BOT)["head"] == client.head
    monkeypatch.setattr(publication, "current", lambda *_: None)
    result = rehearsal.lifecycle(client, {"source": SOURCE}, report, files, BOT, URL, work)
    assert result["status"] == "delivery-passed"
    assert len(client.pulls) == 1


def test_workflow_is_manual_main_only_and_mints_after_retaining_qualification():
    workflow = yaml.safe_load((ROOT / ".github/workflows" / rehearsal.WORKFLOW).read_text())
    assert set(workflow[True]) == {"workflow_dispatch"}
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]
    assert dispatch["exercise_delivery"]["default"] is False
    assert dispatch["case"]["options"] == list(rehearsal.CASES)
    jobs = workflow["jobs"]
    assert "refs/heads/main" in jobs["acquire"]["if"]
    assert "workflow_dispatch" in jobs["acquire"]["if"]
    assert jobs["qualify"]["needs"] == ["acquire", "scan", "checks"]
    steps = jobs["qualify"]["steps"]
    ids = [step.get("id") for step in steps]
    assert ids.index("qualify") < ids.index("retain") < ids.index("app") < ids.index("deliver")
    app = next(step for step in steps if step.get("id") == "app")
    assert "inputs.exercise_delivery" in app["if"]
    assert "delivery_ready == 'true'" in app["if"]
    assert "steps.retain.outcome == 'success'" in app["if"]
    assert "PLATFORM_IMAGE_REMEDIATION_ENABLED" not in json.dumps(workflow)
    for name, job in jobs.items():
        assert all(value == "read" for value in job.get("permissions", {}).values())
        if name != "qualify":
            assert "IMAGE_REMEDIATION_APP_PRIVATE_KEY" not in json.dumps(job)
    assert steps[-1]["if"].startswith("always()")
    assert "rehearsal-failure.json" in steps[-1]["with"]["path"]


@pytest.fixture
def historical(qualified, context, monkeypatch):
    root, work, results = qualified
    source = root / planner.NUGET
    source.write_text(source.read_text().replace('Version="3.1.12"', 'Version="3.1.13"'))
    old = read(work / "receipt.json")["directories"][0]
    (work / "evidence" / old).rename(work / "evidence/bicep-docker-sbx-0.2.2")
    write(
        work / "evidence/bicep-docker-sbx-0.2.2/assessment.json",
        read(work / "evidence/bicep-docker-sbx-0.2.2/observation.json")["assessment"],
    )
    write(work / "evidence/monitor-index.json", {"historical": True})
    manifest = read(root / rehearsal.MANIFEST)
    pinned = manifest["cases"][history.CASE]["fixture"]
    pinned["files"] = {name: digest(work / "evidence" / name) for name in pinned["files"]}
    write(root / rehearsal.MANIFEST, manifest)
    expected = planner.plan(root, [work / "evidence/bicep-docker-sbx-0.2.2"])
    assert not expected["updates"]
    write(work / "plan.json", expected)
    write(work / "receipt.json", history.receipt(root, SOURCE))
    write(
        work / "current/receipt.json",
        {"source": SOURCE, "images": dict.fromkeys(expected["affectedProfiles"])},
    )
    for profile in expected["affectedProfiles"]:
        candidate = results / f"candidate-{profile}"
        identity = read(candidate / "qualification.json")
        identity.update(inputs=expected["inputs"], plan=planner.digest(work / "plan.json"))
        write(candidate / "qualification.json", identity)
        baseline = results / f"baseline-{profile}"
        for path in candidate.iterdir():
            shutil.copyfile(path, baseline / path.name)
        write(baseline / "qualification.json", {**identity, "variant": "baseline"})
    monkeypatch.setattr(history, "git", lambda *_: SOURCE)
    monkeypatch.setenv("REHEARSAL_CASE", history.CASE)
    write(work / "rehearsal-request.json", rehearsal.request_record(root))
    return root, work, results


def test_historical_replay_uses_fresh_builds_and_separate_current_authority(
    historical, monkeypatch
):
    root, work, results = historical
    seen = []
    monkeypatch.setattr(rehearsal, "current", lambda _, value: seen.append(value))
    report = rehearsal.qualify(root, work, results)
    assert report["qualification"]["status"] == "release-needed"
    assert len(report["qualification"]["scans"]) == 6
    assert report["evidenceMode"] == "historical-replay"
    assert report["productionRecovery"] is False
    assert report["fixture"] == history.fixture(root)
    assert seen == [read(work / "current/receipt.json")]
    assert "rehearsalFixture" not in seen[0]


def test_historical_delivery_does_not_depend_on_unrelated_current_proposals(
    historical, monkeypatch
):
    root, work, results = historical
    acquired = read(work / "current/receipt.json")
    acquired["images"]["graphviz"] = {"release": "unrelated"}
    write(work / "current/receipt.json", acquired)
    seen = []
    monkeypatch.setattr(rehearsal, "current", lambda _, value: seen.append(value))
    rehearsal.qualify(root, work, results)
    assert set(seen[0]["images"]) == set(history.BICEP_ROUTES)
    assert read(work / "current/receipt.json") == acquired


@pytest.mark.parametrize(
    "mutation", ["evidence", "index", "plan", "source", "scope", "fixture", "request"]
)
def test_historical_replay_refuses_changed_inputs(historical, mutation):
    root, work, results = historical
    if mutation in {"evidence", "index"}:
        name = (
            "bicep-docker-sbx-0.2.2/grype.json" if mutation == "evidence" else "monitor-index.json"
        )
        path = work / "evidence" / name
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "plan":
        expected = read(work / "plan.json")
        expected["findings"] = []
        write(work / "plan.json", expected)
    elif mutation in {"source", "scope"}:
        receipt = read(work / "current/receipt.json")
        if mutation == "source":
            receipt["source"] = "foreign"
        else:
            del receipt["images"]["bicep-avm"]
        write(work / "current/receipt.json", receipt)
    elif mutation == "fixture":
        receipt = read(work / "receipt.json")
        receipt["rehearsalFixture"]["producer"] = "foreign"
        write(work / "receipt.json", receipt)
    else:
        request = read(work / "rehearsal-request.json")
        request["runAttempt"] += 1
        write(work / "rehearsal-request.json", request)
    with pytest.raises(ValueError):
        rehearsal.qualify(root, work, results)


@pytest.mark.parametrize("phase", ["runtime", "scan", "stale"])
def test_historical_inputs_do_not_waive_new_qualification(historical, monkeypatch, phase):
    root, work, results = historical
    monkeypatch.setattr(rehearsal, "current", lambda *_: None)
    if phase == "runtime":
        path = results / "candidate-bicep/runtime.json"
        value = read(path)
        value["passed"] = False
        write(path, value)
    elif phase == "scan":
        path = results / "candidate-bicep/qualification.json"
        value = read(path)
        value["source"] = "historical-producer"
        write(path, value)
    else:

        def stale(*_):
            raise acquisition.EvidenceChanging("Monitoring changed")

        monkeypatch.setattr(rehearsal, "current", stale)
    with pytest.raises(ValueError):
        rehearsal.qualify(root, work, results)


def test_production_entry_points_refuse_historical_receipts(historical):
    root, work, _ = historical
    with pytest.raises(ValueError, match="isolated rehearsal"):
        rehearsal.platform.validate(root, work)
    with pytest.raises(ValueError, match="not a current-evidence receipt"):
        acquisition.current(FakeGitHub(), read(work / "receipt.json"))


@pytest.mark.parametrize("outcome", ["ready", "deferred", "producer", "tampered"])
def test_historical_acquisition_authenticates_pins_and_current_scope(
    historical, tmp_path, monkeypatch, outcome
):
    root, old_work, _ = historical
    pinned = history.fixture(root)
    work = tmp_path / "acquired"
    observed = []
    monkeypatch.setattr(
        history,
        "monitor_source",
        lambda *_: "wrong" if outcome == "producer" else pinned["producer"],
    )

    def restore(client, monitor, source, targets, destination):
        assert monitor == pinned["monitor"] and source == pinned["producer"]
        assert targets == {"bicep-docker-sbx-0.2.2"}
        shutil.copytree(old_work / "evidence", destination)
        if outcome == "tampered":
            (destination / "monitor-index.json").write_text("{}")

    def acquire(client, source, destination, **kwargs):
        observed.append(kwargs)
        if outcome == "deferred":
            return None
        write(destination / "receipt.json", read(old_work / "current/receipt.json"))
        return {"affectedProfiles": [], "updates": []}

    monkeypatch.setattr(history, "restore_monitor", restore)
    monkeypatch.setattr(history, "acquire_with_retry", acquire)
    monkeypatch.setattr(history, "current", lambda *_: None)
    if outcome == "ready":
        expected = rehearsal.acquire(root, work)
        assert expected == read(old_work / "plan.json")
        assert read(work / "rehearsal-request.json") == rehearsal.request_record(root)
        assert observed == [
            {
                "selected_profiles": history.BICEP_ROUTES,
                "planner": history.plan,
                "require_all_profiles": False,
            }
        ]
        with pytest.raises(FileExistsError):
            rehearsal.acquire(root, work)
    else:
        with pytest.raises(ValueError):
            rehearsal.acquire(root, work)
        if outcome in {"producer", "tampered"}:
            assert not observed


def test_historical_delivery_receipt_labels_its_evidence(historical, monkeypatch):
    root, work, results = historical
    monkeypatch.setattr(rehearsal, "current", lambda *_: None)
    qualified = rehearsal.qualify(root, work, results)
    _, files = rehearsal.delivery_payload(root, qualified, URL)
    receipt = json.loads(files[publication.REHEARSAL_PATH])
    assert receipt["evidenceMode"] == "historical-replay"
    assert receipt["fixture"] == history.fixture(root)
    assert receipt["productionRecovery"] is False


def test_historical_delivery_cli_passes_only_current_authority_to_publisher(
    historical, monkeypatch
):
    root, work, results = historical
    monkeypatch.setattr(rehearsal, "ROOT", root)
    monkeypatch.setattr(rehearsal, "current", lambda *_: None)
    monkeypatch.setenv("REHEARSAL_DELIVERY", "true")
    write(work / "rehearsal-qualified.json", rehearsal.qualify(root, work, results))
    seen = []
    monkeypatch.setattr(rehearsal, "lifecycle", lambda *args: seen.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rehearsal",
            "deliver",
            "--work",
            str(work),
            "--results",
            str(results),
            "--bot",
            BOT,
            "--artifact-id",
            "456",
        ],
    )
    rehearsal.main()
    assert len(seen) == 1
    assert seen[0][1] == read(work / "current/receipt.json")
    assert "rehearsalFixture" not in seen[0][1]
    assert seen[0][2]["evidenceMode"] == "historical-replay"
