"""The sbx live suites must select the guest image built with their required utilities."""

from __future__ import annotations

from pathlib import Path

import pytest
import test_sbx_codeact_e2e as codeact
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.workflow


def test_workflow_loads_and_retains_the_selected_shell_image():
    workflow = yaml.safe_load((ROOT / ".github/workflows/sbx-live.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["sbx-e2e"]
    assert job["env"]["MAF_SANDBOX_SBX_E2E_IMAGE"] == "maf-sbx-e2e-shell:local"
    steps = job["steps"]
    load = next(step for step in steps if step.get("name") == "Load the test templates")
    assert "for image in shell agent root; do" in load["run"]
    assert 'sbx template load "$RUNNER_TEMP/$image.tar"' in load["run"]
    assert 'docker image inspect "$MAF_SANDBOX_SBX_E2E_IMAGE" > shell-image.json' in load["run"]
    for name in ("Run the host-state refusals and faults", "Run the live suite"):
        step = next(step for step in steps if step.get("name") == name)
        assert steps.index(load) < steps.index(step)
        assert "MAF_SANDBOX_SBX_E2E_IMAGE" not in step.get("env", {})
    retain = next(step for step in steps if step.get("name") == "Retain live evidence")
    assert retain["if"] == "always()"
    assert "shell-image.json" in retain["with"]["path"].splitlines()


@pytest.mark.parametrize("job_name", ["sbx-e2e", "pricing-e2e"])
def test_host_probes_have_an_ephemeral_signing_key_and_retained_evidence(job_name):
    workflow = yaml.safe_load((ROOT / ".github/workflows/sbx-live.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    probe = next(step for step in steps if "test_sbx_e2e_host.py" in step.get("run", ""))
    script = probe["run"]
    assert 'eval "$(ssh-agent -s)"' in script
    assert "ssh-keygen -q -t ed25519" in script
    assert "ssh-add " in script
    assert "export MAF_SANDBOX_SBX_E2E_AGENT_PUBLIC_KEY=" in script
    assert "trap 'ssh-agent -k" in script and "' EXIT" in script
    assert "set -euo pipefail" in script
    log = "pricing-host.log" if job_name == "pricing-e2e" else "host.log"
    assert f"| tee {log}" in script
    retain = next(step for step in steps if step.get("name") == "Retain live evidence")
    assert retain["if"] == "always()"
    assert log in retain["with"]["path"].splitlines()


@pytest.mark.parametrize("image", [None, "prepared-shell:test"])
def test_codeact_uses_the_prepared_guest_image(monkeypatch, tmp_path, image):
    if image is None:
        monkeypatch.delenv("MAF_SANDBOX_SBX_E2E_IMAGE", raising=False)
    else:
        monkeypatch.setenv("MAF_SANDBOX_SBX_E2E_IMAGE", image)
    observed = []

    class ImageSelected(Exception):
        pass

    def make_tools(*args, **kwargs):
        observed.append(kwargs["image"])
        raise ImageSelected

    monkeypatch.setattr(codeact, "make_codeact_tools", make_tools)
    with pytest.raises(ImageSelected):
        codeact.test_codeact_receives_a_real_host_effect(tmp_path)
    assert observed == [image or "maf-sbx-e2e-shell:local"]
