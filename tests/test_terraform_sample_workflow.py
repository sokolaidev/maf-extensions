"""Four named jobs exercise sample 20 for every affected package release."""

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))


@pytest.mark.parametrize("backend", ["docker", "acas"])
@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_sample_job_selects_its_backend_engine_and_published_source(backend, engine):
    job = WORKFLOW["jobs"][f"sample-20-{backend}-{engine}"]
    assert "inputs.package == ''" in job["if"]
    assert "inputs.source" not in job["if"]
    selected = re.search(r"fromJSON\('([^']+)'\)", job["if"])
    assert selected
    assert set(json.loads(selected[1])) == {
        "maf-sandbox",
        f"maf-sandbox-{backend}",
        "maf-sandbox-terraform",
    }
    assert job.get("needs") == ("acas-images" if backend == "acas" else None)
    env = job["env"]
    assert env["SAMPLE_BACKEND"] == backend and env["SAMPLE_ENGINE"] == engine
    pins = json.loads((ROOT / "images/terraform-sandbox/image.json").read_text())
    assert env["ENGINE_VERSION"] == pins["engines"][engine]["version"]
    image = f"{engine.upper()}_SANDBOX_IMAGE"
    if backend == "acas":
        assert env[image] == "${{ vars." + image + " }}"
    steps = job["steps"]
    preflight = next(
        step for step in steps if step.get("name") == "Check the live configuration is present"
    )
    login = next(step for step in steps if "azure/login@" in step.get("uses", ""))
    assert steps.index(preflight) < steps.index(login)
    loop = re.search(r"for name in (.*?); do", preflight["run"], re.DOTALL)
    assert loop
    variables = set(loop[1].replace("\\", "").split())
    assert {
        image,
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_CHAT_MODEL",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    } <= variables
    assert variables <= (env | preflight["env"]).keys()
    run = next(step["run"] for step in steps if "uv run --no-project" in step.get("run", ""))
    assert "set -euo pipefail" in run and "2>&1 | tee" in run
    assert "uv run --no-project $source_args samples/20_terraform_validation/agent.py" in run
    assert '"$HARNESS"/scripts/check_live_terraform_sample.py' in run
    assert (
        '--backend "$SAMPLE_BACKEND" --engine "$SAMPLE_ENGINE" --version "$ENGINE_VERSION"' in run
    )
    build = [step for step in steps if "build_image.py" in step.get("run", "")]
    assert len(build) == int(backend == "docker")
    if build:
        assert '--engine "$SAMPLE_ENGINE" --profile random' in build[0]["run"]
        assert f'--tag "${image}"' in build[0]["run"]
    artifacts = [step for step in steps if "actions/upload-artifact@" in step.get("uses", "")]
    assert len(artifacts) == 1 and artifacts[0]["if"] == "always()"
    assert backend in artifacts[0]["with"]["name"] and engine in artifacts[0]["with"]["name"]
    assert not job.get("continue-on-error")


def test_terraform_release_dispatches_live_verification():
    publish = yaml.safe_load((ROOT / ".github/workflows/publish-packages.yml").read_text("utf-8"))
    conditions = [
        publish["jobs"][name]["if"] for name in ("wait-for-propagation", "train-status", "verify")
    ]
    assert len(set(conditions)) == 1
    assert '"maf-sandbox-terraform"' in conditions[0]


def test_shared_preflight_checks_both_engine_variables_only_when_source_has_sample():
    job = WORKFLOW["jobs"]["acas-images"]
    assert '"maf-sandbox-terraform"' in job["if"]
    run = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Check the live configuration is present"
    )
    assert "-f samples/20_terraform_validation/agent.py" in run
    assert "for name in TERRAFORM_SANDBOX_IMAGE OPENTOFU_SANDBOX_IMAGE" in run
    for engine in ("TERRAFORM", "OPENTOFU"):
        variable = f"{engine}_SANDBOX_IMAGE"
        assert job["env"][variable] == "${{ vars." + variable + " }}"
