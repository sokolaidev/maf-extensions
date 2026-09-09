"""Live image prerequisites fail before model calls and cover both ACAS image namespaces."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from azure.containerapps.sandbox import DiskImage, DiskImageSpec, PublicDiskImage

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_acas_live_images", _ROOT / "scripts/check_acas_live_images.py"
)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)

_ENV = {"ACAS_SANDBOX_REGISTRY": "example.azurecr.io", "BICEP_SANDBOX_IMAGE": "bicep:1"}
_BICEP = "example.azurecr.io/bicep:1"
_CODEACT = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"


class _Group:
    def __init__(self, images=(), prebuilt=()):
        self.images = images
        self.prebuilt = prebuilt
        self.calls = []
        self.failure = None

    def list_disk_images(self):
        self.calls.append("imports")
        yield from self.images
        if self.failure:
            raise self.failure

    def list_public_disk_images(self):
        self.calls.append("catalogue")
        yield from self.prebuilt
        if self.failure:
            raise self.failure


def _disk(reference, id="disk"):
    return DiskImage(id=id, image=DiskImageSpec(base=reference))


def test_all_images_are_collected_with_shared_consumers_and_an_optional_skip():
    required, skipped = check.required_images(_ROOT, "", _ENV)
    assert set(required) == {_BICEP, _CODEACT, "python-3.13"}
    assert required[_CODEACT] == ["14_acas_codeact_files", "15_acas_codeact_host_tools"]
    assert required["python-3.13"] == ["03_acas_codeact", "acas-e2e prebuilt"]
    assert skipped == ["ACAS_SANDBOX_NONROOT_IMAGE is unset; the optional non-root leg skips."]


@pytest.mark.parametrize(
    ("package", "references"),
    [
        ("maf-sandbox", {_BICEP, _CODEACT, "python-3.13"}),
        ("maf-sandbox-acas", {_BICEP, _CODEACT, "python-3.13"}),
        ("maf-sandbox-bicep", {_BICEP}),
        ("maf-sandbox-codeact", {_CODEACT, "python-3.13"}),
        ("maf-sandbox-docker", set()),
        ("maf-sandbox-wslc", set()),
        ("maf-sandbox-otel", set()),
    ],
)
def test_package_selection(package, references):
    required, _ = check.required_images(_ROOT, package, _ENV)
    assert set(required) == references


def test_codeact_does_not_require_bicep_configuration_or_inspect_optional_nonroot():
    required, skipped = check.required_images(
        _ROOT, "maf-sandbox-codeact", {"ACAS_SANDBOX_NONROOT_IMAGE": "absent:1"}
    )
    assert set(required) == {_CODEACT, "python-3.13"}
    assert skipped == []


def test_configured_nonroot_and_prebuilt_override_are_checked():
    env = {
        **_ENV,
        "ACAS_SANDBOX_NONROOT_IMAGE": "nonroot:2",
        "MAF_SANDBOX_ACAS_E2E_PREBUILT": "ubuntu",
    }
    required, skipped = check.required_images(_ROOT, "", env)
    assert required["example.azurecr.io/nonroot:2"] == ["acas-e2e non-root"]
    assert required["ubuntu"] == ["acas-e2e prebuilt"]
    assert skipped == []
    _, failures = check.check_images(_Group(), required)
    assert any("nonroot:2" in failure for failure in failures)


def test_source_under_test_supplies_each_image_without_executing_modules(tmp_path):
    for sample in check._CODEACT_SAMPLES:
        source = tmp_path / "samples" / sample / "agent.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            f'raise RuntimeError("must not execute")\nCODEACT_IMAGE: str = "{sample}:new"\n',
            encoding="utf-8",
        )
    required, _ = check.required_images(tmp_path, "maf-sandbox-codeact", {})
    assert set(required) == {f"{sample}:new" for sample in check._CODEACT_SAMPLES}


@pytest.mark.parametrize(
    "source",
    [
        "CODEACT_IMAGE = choose_image()\n",
        'OTHER_IMAGE = "python-3.13"\n',
        'CODEACT_IMAGE = ""\n',
        'CODEACT_IMAGE = "one"\nCODEACT_IMAGE = "two"\n',
    ],
)
def test_unreadable_image_source_fails_instead_of_using_a_stale_default(tmp_path, source):
    path = tmp_path / "agent.py"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read CODEACT_IMAGE"):
        check._image_constant(path, "CODEACT_IMAGE", {})


def test_bicep_missing_variables_are_reported_together():
    with pytest.raises(ValueError, match="ACAS_SANDBOX_REGISTRY, BICEP_SANDBOX_IMAGE"):
        check.required_images(_ROOT, "maf-sandbox-bicep", {})


def test_inventory_reads_once_per_namespace_and_matches_spec_base():
    required, _ = check.required_images(_ROOT, "", _ENV)
    client = _Group(
        [_disk(_BICEP), _disk(_CODEACT)],
        [PublicDiskImage(name="python-3.13")],
    )
    present, failures = check.check_images(client, required)
    assert len(present) == 3
    assert failures == []
    assert client.calls == ["imports", "catalogue"]


def test_all_missing_assets_report_consumers_import_command_or_catalogue():
    required, _ = check.required_images(_ROOT, "", _ENV)
    _, failures = check.check_images(_Group(prebuilt=[PublicDiskImage(name="ubuntu")]), required)
    assert len(failures) == 3
    imported = next(failure for failure in failures if _CODEACT in failure)
    assert "14_acas_codeact_files, 15_acas_codeact_host_tools" in imported
    assert "packages/maf-sandbox-acas/scripts/import_disk_image.py" in imported
    assert f"--image {_CODEACT}" in imported
    assert '--group "$ACAS_SANDBOX_GROUP"' in imported
    catalogue = next(failure for failure in failures if "Missing prebuilt" in failure)
    assert "Service catalogue: ubuntu" in catalogue
    assert "import_disk_image.py" not in catalogue


def test_identical_names_in_wrong_namespaces_do_not_satisfy_requirements():
    client = _Group([_disk("python-3.13")], [PublicDiskImage(name=_CODEACT)])
    _, failures = check.check_images(client, {_CODEACT: ["files"], "python-3.13": ["prebuilt"]})
    assert len(failures) == 2


@pytest.mark.parametrize(
    ("images", "failure"),
    [
        ([_disk(_CODEACT, "one"), _disk(_CODEACT, "two")], "Ambiguous"),
        ([_disk(_CODEACT, "")], "Missing"),
        ([SimpleNamespace(id="one", image=None)], "Missing"),
        ([_disk(_CODEACT, "one"), _disk(_CODEACT, "one")], None),
        ([SimpleNamespace(id="one", image=_CODEACT)], None),
    ],
)
def test_import_identity_and_sdk_shapes(images, failure):
    _, failures = check.check_images(_Group(images), {_CODEACT: ["files"]})
    assert len(failures) == (1 if failure else 0)
    if failure:
        assert failure in failures[0]


def test_single_namespace_does_not_list_the_other():
    client = _Group([_disk(_BICEP)])
    assert check.check_images(client, {_BICEP: ["bicep"]})[1] == []
    assert client.calls == ["imports"]


@pytest.fixture
def cli(monkeypatch):
    for variable in [*check._CONFIG.values(), "ACAS_SANDBOX_REGISTRY", "BICEP_SANDBOX_IMAGE"]:
        monkeypatch.setenv(variable, _ENV.get(variable, "configured"))
    monkeypatch.delenv("ACAS_SANDBOX_NONROOT_IMAGE", raising=False)
    monkeypatch.delenv("MAF_SANDBOX_ACAS_E2E_PREBUILT", raising=False)
    client = _Group([_disk(_BICEP), _disk(_CODEACT)], [PublicDiskImage(name="python-3.13")])
    events = []

    @contextmanager
    def credential():
        events.append("credential opened")
        try:
            yield "credential"
        finally:
            events.append("credential closed")

    @contextmanager
    def group(**kwargs):
        assert kwargs["credential"] == "credential"
        assert set(kwargs) == {*check._CONFIG, "credential"}
        events.append("group opened")
        try:
            yield client
        finally:
            events.append("group closed")

    monkeypatch.setattr(check, "AzureCliCredential", credential)
    monkeypatch.setattr(check, "SandboxGroupClient", group)
    return client, events


def test_cli_success_reports_optional_skip_and_freshness_limit(cli, tmp_path, capsys):
    _, events = cli
    summary = tmp_path / "summary.md"
    assert check.main(["--source-root", str(_ROOT), "--summary", str(summary)]) == 0
    output = capsys.readouterr().out
    assert "::error::" not in output
    assert "ACAS_SANDBOX_NONROOT_IMAGE is unset" in summary.read_text("utf-8")
    assert "does not verify snapshot freshness" in output
    assert events == ["credential opened", "group opened", "group closed", "credential closed"]


def test_cli_missing_images_fails_and_writes_actionable_summary(cli, tmp_path, capsys):
    client, _ = cli
    client.images = []
    summary = tmp_path / "summary.md"
    assert check.main(["--source-root", str(_ROOT), "--summary", str(summary)]) == 1
    assert capsys.readouterr().out.count("::error::") == 2
    assert "import_disk_image.py" in summary.read_text("utf-8")


def test_incomplete_inventory_fails_and_closes_clients(cli, tmp_path, capsys):
    client, events = cli
    client.failure = RuntimeError("listing failed\n100% incomplete")
    summary = tmp_path / "summary.md"
    assert check.main(["--source-root", str(_ROOT), "--summary", str(summary)]) == 1
    assert "listing failed%0A100%25 incomplete" in capsys.readouterr().out
    assert "could not complete" in summary.read_text("utf-8")
    assert events[-2:] == ["group closed", "credential closed"]


def test_missing_group_configuration_never_opens_a_client(cli, monkeypatch, capsys):
    _, events = cli
    monkeypatch.delenv("ACAS_SANDBOX_GROUP")
    assert check.main(["--source-root", str(_ROOT)]) == 1
    assert "ACAS_SANDBOX_GROUP" in capsys.readouterr().out
    assert events == []


def test_non_acas_selection_never_opens_a_client(cli):
    _, events = cli
    assert check.main(["--package", "maf-sandbox-docker"]) == 0
    assert events == []


def _workflow(name):
    return yaml.safe_load((_ROOT / ".github/workflows" / name).read_text("utf-8"))


def _admits(job, package):
    condition = job["if"]
    match = re.search(r"fromJSON\('([^']+)'\)", condition)
    return not package or (match is not None and package in json.loads(match[1]))


def test_preflight_blocks_exactly_the_acas_jobs_and_admits_their_package_union():
    jobs = _workflow("verify-live.yml")["jobs"]
    consumers = {"sample-01", "sample-03", "sample-14", "sample-15", "acas-e2e"}
    for name, job in jobs.items():
        assert (job.get("needs") == "acas-images") == (name in consumers)
        if name in consumers:
            assert "always()" not in job["if"]
            assert "continue-on-error" not in job
    preflight = jobs["acas-images"]
    assert "continue-on-error" not in preflight
    for package in ["", *(path.name for path in (_ROOT / "packages").iterdir() if path.is_dir())]:
        assert _admits(preflight, package) == any(
            _admits(jobs[name], package) for name in consumers
        )
        required, _ = check.required_images(_ROOT, package, _ENV)
        assert bool(required) == _admits(preflight, package)
        selected = {consumer for consumers in required.values() for consumer in consumers}
        for job, consumer in [
            ("sample-03", "03_acas_codeact"),
            ("sample-14", "14_acas_codeact_files"),
            ("sample-15", "15_acas_codeact_host_tools"),
            ("acas-e2e", "acas-e2e prebuilt"),
        ]:
            assert (consumer in selected) == _admits(jobs[job], package)


def test_tag_preflight_uses_the_harness_but_reads_the_source_under_test():
    job = _workflow("verify-live.yml")["jobs"]["acas-images"]
    steps = job["steps"]
    harness = next(step for step in steps if step.get("with", {}).get("path") == ".harness")
    assert harness["if"] == "startsWith(github.ref, 'refs/tags/')"
    assert "default_branch" in harness["with"]["ref"]
    assert "sparse-checkout" not in harness["with"]
    checker = next(
        i for i, step in enumerate(steps) if "check_acas_live_images.py" in step.get("run", "")
    )
    login = next(
        i for i, step in enumerate(steps) if step.get("uses", "").startswith("azure/login@")
    )
    assert login < checker
    assert '--project "$HARNESS"' in steps[checker]["run"]
    assert 'python "$HARNESS"/scripts/check_acas_live_images.py' in steps[checker]["run"]
    assert '--source-root "$GITHUB_WORKSPACE"' in steps[checker]["run"]
    assert steps[checker]["env"]["PACKAGE"] == "${{ inputs.package }}"
    assert '--package "$PACKAGE"' in steps[checker]["run"]
    assert "continue-on-error" not in steps[checker]


def test_daily_check_covers_all_images_before_any_sandbox_is_created():
    workflow = _workflow("conformance-live.yml")
    assert "schedule" in workflow.get("on", workflow.get(True))
    steps = workflow["jobs"]["acas-conformance"]["steps"]
    preflight = next(
        i for i, step in enumerate(steps) if "check_acas_live_images.py" in step.get("run", "")
    )
    login = next(
        i for i, step in enumerate(steps) if step.get("uses", "").startswith("azure/login@")
    )
    suite = next(i for i, step in enumerate(steps) if "pytest" in step.get("run", ""))
    assert login < preflight < suite
    assert "--package" not in steps[preflight]["run"]
    assert "continue-on-error" not in steps[preflight]
    assert "if" not in steps[preflight]
    env = steps[preflight]["env"]
    assert set(check._CONFIG.values()) <= set(env)
    assert env["ACAS_SANDBOX_NONROOT_IMAGE"] == "${{ vars.ACAS_SANDBOX_NONROOT_IMAGE }}"
