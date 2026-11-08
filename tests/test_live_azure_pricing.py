"""Retain failed live-pricing evidence without Docker or external pricing requests."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agent_framework import Content
from maf_sandbox import Isolation, ToolCallEnded
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_container_release_image as release_check  # noqa: E402
import check_live_azure_pricing as pricing  # noqa: E402
import container_release_publish as publisher  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE = "sha256:" + "a" * 64
PROXY = "sha256:" + "b" * 64


@pytest.fixture
def pricing_host(monkeypatch, tmp_path):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(pricing.DockerSandboxBackend, "create", AsyncMock(return_value=backend))
    monkeypatch.setattr(pricing, "docker", lambda *args: args[2])
    monkeypatch.setattr(pricing, "remaining", lambda scope: ([], []))
    cleanup = AsyncMock(return_value=SimpleNamespace(undisposed=()))
    monkeypatch.setattr(pricing.SandboxRouter, "dispose_scope", cleanup)

    async def process(context, next):
        await next()

    for middleware in ("LabelTrackingFunctionMiddleware", "PolicyEnforcementFunctionMiddleware"):
        monkeypatch.setattr(pricing, middleware, lambda **kwargs: SimpleNamespace(process=process))

    def install(result, *, successful_prefix=False):
        def tools(router, *args, **kwargs):
            count = 0

            async def invoke(*, context):
                nonlocal count
                count += 1
                router.observer.tool_call_ended(
                    ToolCallEnded(
                        tool="azure_pricing_get",
                        kind="azure-pricing",
                        keys=(),
                        seconds=90.0,
                        failure=None,
                        unclean=0,
                        call=str(count),
                    )
                )
                if successful_prefix and count <= 3:
                    row = {
                        "priceType": context.arguments["price_type"],
                        "currencyCode": "USD",
                        "retailPrice": 1,
                        "region": "westeurope",
                        "matched_by": "region",
                        "armSkuName": "Standard_D4s_v5",
                        "reservationTerm": "1 Year",
                        "savingsPlan": [{"retailPrice": 0.5}],
                    }
                    if count == 3:
                        row.update(
                            serviceName="Virtual Network",
                            productName="Virtual Network Private Link",
                            region="Global",
                            matched_by="global",
                        )
                    report = Content.from_text(json.dumps({"prices": [row], "truncated": False}))
                    report.additional_properties["security_label"] = {
                        "integrity": "trusted",
                        "confidentiality": "public",
                    }
                    return [Content.from_text(pricing.COMPLETED_TEXT), report]
                if isinstance(result, Exception):
                    raise result
                return result

            return [SimpleNamespace(additional_properties={}, invoke=invoke)]

        monkeypatch.setattr(pricing, "make_azure_pricing_tools", tools)

    return install, cleanup


def test_network_zone_failure_retains_tool_result_and_completed_checks(
    pricing_host, tmp_path, capsys
):
    install, cleanup = pricing_host
    message = "Pricing lookup incomplete: the CLI did not return a clean result."
    install([Content.from_text("Incomplete"), Content.from_text(message)], successful_prefix=True)
    directory = tmp_path / "azure-pricing"

    async def dispose(*args):
        assert (directory / "failure.json").is_file()
        return SimpleNamespace(undisposed=())

    cleanup.side_effect = dispose
    with pytest.raises(ValueError, match="Pricing tool did not complete: network-zone"):
        asyncio.run(pricing.check(IMAGE, PROXY, directory=directory))

    failure = json.loads((directory / "failure.json").read_text("utf-8"))
    assert failure["passed"] is False
    assert failure["case"] == "network-zone"
    assert failure["imageId"] == IMAGE
    assert failure["proxyImageId"] == PROXY
    assert "Virtual Network Manager" in failure["arguments"]["filter"]
    assert failure["toolResult"] == ["Incomplete", message]
    assert [case["case"] for case in failure["checks"]] == [
        "Reservation",
        "Consumption",
        "private-endpoint",
    ]
    assert failure["events"] == [{"seconds": 90.0, "failure": None, "unclean": 0}]
    assert message in capsys.readouterr().out
    cleanup.assert_awaited_once()


@pytest.mark.parametrize(
    "result", [[], None, "unexpected result", RuntimeError("transport failed")]
)
def test_malformed_or_raised_result_still_retains_failure(pricing_host, tmp_path, result):
    install, cleanup = pricing_host
    install(result)
    expected = RuntimeError if isinstance(result, Exception) else ValueError
    with pytest.raises(expected):
        asyncio.run(pricing.check(IMAGE, PROXY, directory=tmp_path))
    failure = json.loads((tmp_path / "failure.json").read_text("utf-8"))
    assert failure["case"] == "Reservation"
    assert failure["error"]["type"] == expected.__name__
    assert failure["checks"] == []
    cleanup.assert_awaited_once()


def test_tool_diagnostics_bound_text_and_exclude_metadata():
    item = Content.from_text("x" * 5000)
    item.additional_properties["private_metadata"] = "not diagnostic text"
    assert pricing.result_text([item] * 20) == ["x" * 4096] * 16


def test_release_retains_failed_pricing_without_qualification(monkeypatch, tmp_path, pricing_host):
    install, _ = pricing_host
    install([Content.from_text("Pricing execution timed out")])
    monkeypatch.setattr(pricing.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(publisher, "selected", lambda directory: {"profile": "azure-pricing"})
    monkeypatch.setattr(publisher, "verify_candidate", lambda *a: {})
    monkeypatch.setattr(publisher, "anonymous_pull", lambda candidate: IMAGE)
    monkeypatch.setattr(publisher, "check", release_check.check)
    with pytest.raises(ValueError, match="Pricing tool did not complete"):
        publisher.qualify(tmp_path)
    assert (tmp_path / "azure-pricing/failure.json").is_file()
    assert not (tmp_path / "qualification.json").exists()


def test_workflow_retains_failed_evidence_separately_from_success():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    steps = workflow["jobs"]["qualify"]["steps"]
    qualification = next(step for step in steps if step.get("id") == "qualification")
    failed = next(
        step for step in steps if step.get("name") == "Retain failed qualification evidence"
    )
    success = next(
        step for step in steps if step.get("with", {}).get("name") == "qualified-image-evidence"
    )
    assert failed["if"] == "failure() && steps.qualification.outcome == 'failure'"
    assert (
        failed["with"]["name"]
        == "failed-qualification-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert failed["with"]["path"] == success["with"]["path"]
    assert "if" not in success
    assert steps.index(qualification) < steps.index(failed) < steps.index(success)
