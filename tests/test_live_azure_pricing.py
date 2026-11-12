"""Qualify pricing trust boundaries and retain failed live evidence without Docker."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agent_framework import Content, FunctionInvocationContext
from agent_framework.security import (
    LabelTrackingFunctionMiddleware,
    PolicyEnforcementFunctionMiddleware,
)
from maf_sandbox import (
    CallerContext,
    Capability,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxKey,
    SandboxRouter,
    ToolCallEnded,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_container_release_image as release_check  # noqa: E402
import check_live_azure_pricing as pricing  # noqa: E402
import container_release_publish as publisher  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE = "sha256:" + "a" * 64
PROXY = "sha256:" + "b" * 64


@pytest.fixture
def pricing_host(monkeypatch):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(pricing.DockerSandboxBackend, "create", AsyncMock(return_value=backend))

    def docker(*args):
        if args[0] == "inspect":
            return json.dumps([{"Image": IMAGE, "Id": args[1]}])
        return args[2]

    monkeypatch.setattr(pricing, "docker", docker)
    monkeypatch.setattr(pricing, "remaining", lambda scope: ([], []))
    monkeypatch.setattr(
        pricing.SandboxRouter,
        "acquire",
        AsyncMock(side_effect=lambda key, spec: SimpleNamespace(instance_id=key.call_id)),
    )
    probe = SimpleNamespace(
        instance_id="probe",
        exec=AsyncMock(return_value=ExecResult(stdout='{"denied": true}', exit_code=0)),
    )
    monkeypatch.setattr(backend, "acquire", AsyncMock(return_value=probe))
    monkeypatch.setattr(backend, "dispose", AsyncMock(return_value=None))
    cleanup = AsyncMock(return_value=SimpleNamespace(undisposed=()))
    monkeypatch.setattr(pricing.SandboxRouter, "dispose_scope", cleanup)

    async def process(context, next):
        await next()

    for middleware in ("LabelTrackingFunctionMiddleware", "PolicyEnforcementFunctionMiddleware"):
        monkeypatch.setattr(pricing, middleware, lambda **kwargs: SimpleNamespace(process=process))

    def install(result, *, successful_queries=0):
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
                if count <= successful_queries:
                    await router.acquire(
                        SandboxKey("scope", "user", "agent", str(count)),
                        pricing.azure_pricing_sandbox_spec(IMAGE),
                    )
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
                    if count >= 3:
                        service, product, location, family = [
                            ("Virtual Network", "Virtual Network Private Link", "Global", None),
                            ("Virtual Network", "Virtual Network Manager", "Zone 1", "network"),
                            (
                                "Azure Front Door Service",
                                "Azure Front Door",
                                "Zone 6",
                                "front_door",
                            ),
                            (
                                "Azure Front Door Service",
                                "Azure Front Door Service",
                                "Zone 1",
                                "cdn_classic",
                            ),
                            ("Azure DNS", "Azure DNS", "Zone 1", "dns"),
                        ][count - 3]
                        row.update(
                            serviceName=service,
                            productName=product,
                            region=location,
                            matched_by="global" if family is None else "zone",
                        )
                        if family is not None:
                            row.update(zone_family=family, zone=location)
                    return pricing_result(row)
                if isinstance(result, Exception):
                    raise result
                return result

            return [SimpleNamespace(additional_properties={}, invoke=invoke)]

        monkeypatch.setattr(pricing, "make_azure_pricing_tools", tools)

    return install, cleanup


def pricing_result(row=None):
    row = {"priceType": "Reservation", "currencyCode": "USD", "retailPrice": 1} | (row or {})
    row = {"row": 1, "unitPrice": row["retailPrice"], **row}
    checked = {
        key: row[key] for key in ("row", "unitPrice", "retailPrice", "priceType", "currencyCode")
    }
    items = [Content.from_text(pricing.COMPLETED_TEXT)]
    for key, data, integrity in (
        ("prices", row, "untrusted"),
        ("checked_prices", checked, "trusted"),
    ):
        item = Content.from_text(json.dumps({key: [data], "truncated": False}))
        item.additional_properties["security_label"] = {
            "integrity": integrity,
            "confidentiality": "public",
        }
        items.append(item)
    return items


@pytest.mark.parametrize("part", [1, 2])
@pytest.mark.parametrize("field,value", [("integrity", "wrong"), ("confidentiality", "private")])
def test_qualifier_rejects_wrong_report_labels(part, field, value):
    items = pricing_result()
    items[part].additional_properties["security_label"][field] = value
    with pytest.raises(ValueError, match="label on"):
        pricing.verified_pricing_report(items)


@pytest.mark.parametrize("part", [1, 2])
def test_qualifier_rejects_missing_or_duplicate_reports(part):
    items = pricing_result()
    missing = items[:part] + items[part + 1 :]
    for malformed in (missing, items + [items[part]]):
        with pytest.raises(ValueError, match="Expected one"):
            pricing.verified_pricing_report(malformed)


@pytest.mark.parametrize(
    "mutation",
    ["missing-row", "row-number", "price", "summary", "identity", "missing-price"],
)
def test_qualifier_rejects_mismatched_or_unchecked_prices(mutation):
    items = pricing_result({"productName": "untrusted product"})
    checked = json.loads(items[2].text)
    row = checked["checked_prices"][0]
    if mutation == "missing-row":
        checked["checked_prices"] = []
    elif mutation == "row-number":
        row["row"] = 2
    elif mutation == "price":
        row["retailPrice"] = 9
    elif mutation == "summary":
        checked["truncated"] = True
    elif mutation == "identity":
        row["productName"] = "untrusted product"
    else:
        del row["retailPrice"]
    items[2].text = json.dumps(checked)
    with pytest.raises(ValueError):
        pricing.verified_pricing_report(items)


def test_qualifier_accepts_real_pricing_contract_through_framework_middleware():
    row = {
        "armSkuName": "Standard_D4s_v5",
        "productName": "Untrusted product text",
        "region": "westeurope",
        "currencyCode": "USD",
        "unitOfMeasure": "1 Hour",
        "retailPrice": 1,
        "unitPrice": 1,
        "priceType": "Reservation",
        "reservationTerm": "1 Year",
    }
    sandbox = InProcessSandbox(
        default_stdout=json.dumps({"status": 200, "results": {"prices": [row]}})
    )
    backend = InProcessSandboxBackend(
        sandbox,
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC, Capability.EGRESS_METHODS}),
            egress_method_tokens=None,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    [tool] = pricing.make_azure_pricing_tools(
        router,
        "agent",
        CallerContext(lambda: "scope", lambda: "user", pricing.list_no_files),
        outbound_max_confidentiality="public",
    )
    tool.additional_properties["confidentiality"] = "public"

    async def exercise():
        policy = PolicyEnforcementFunctionMiddleware(block_on_violation=True)
        for _ in range(2):
            tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
            context = FunctionInvocationContext(function=tool, arguments={"region": "westeurope"})

            async def invoke():
                context.result = await tool.invoke(context=context)

            async def enforce():
                await policy.process(context, invoke)

            await tracker.process(context, enforce)
            report = pricing.verified_pricing_report(context.result)
            assert report["prices"][0]["productName"] == row["productName"]
            assert report["prices"][0]["retailPrice"] == 1

    asyncio.run(exercise())
    assert len(backend.disposed) == 2


def test_network_zone_failure_retains_tool_result_and_completed_checks(
    pricing_host, tmp_path, capsys
):
    install, cleanup = pricing_host
    message = "Pricing lookup incomplete: the CLI did not return a clean result."
    install([Content.from_text("Incomplete"), Content.from_text(message)], successful_queries=3)
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


@pytest.mark.parametrize("raises", [False, True])
def test_scope_cleanup_failure_retains_all_passed_checks(pricing_host, tmp_path, raises):
    install, cleanup = pricing_host
    install(None, successful_queries=7)
    if raises:
        cleanup.side_effect = RuntimeError("cleanup transport failed")
    else:
        cleanup.return_value = SimpleNamespace(undisposed=("remaining",))
    message = "cleanup transport failed" if raises else "Pricing scope cleanup failed"
    with pytest.raises((RuntimeError, ValueError), match=message):
        asyncio.run(pricing.check(IMAGE, PROXY, directory=tmp_path))

    failure = json.loads((tmp_path / "failure.json").read_text("utf-8"))
    assert failure["case"] == "scope-cleanup"
    assert failure["passed"] is False
    assert failure["imageId"] == IMAGE
    assert failure["proxyImageId"] == PROXY
    assert failure["error"]["message"] == message
    assert len(failure["checks"]) == 9
    assert failure["arguments"] == {}
    assert failure["toolResult"] == []
    assert failure["events"] == []
    cleanup.assert_awaited_once()


@pytest.mark.parametrize("raises", [False, True])
def test_scope_cleanup_failure_preserves_prior_tool_failure(pricing_host, tmp_path, raises):
    install, cleanup = pricing_host
    install([Content.from_text("Pricing execution timed out")], successful_queries=3)

    async def dispose(*args):
        failure = json.loads((tmp_path / "failure.json").read_text("utf-8"))
        assert failure["case"] == "network-zone"
        assert "cleanupError" not in failure
        if raises:
            raise RuntimeError("cleanup transport failed")
        return SimpleNamespace(undisposed=("remaining",))

    cleanup.side_effect = dispose
    message = "cleanup transport failed" if raises else "Pricing scope cleanup failed"
    with pytest.raises((RuntimeError, ValueError), match=message):
        asyncio.run(pricing.check(IMAGE, PROXY, directory=tmp_path))

    failure = json.loads((tmp_path / "failure.json").read_text("utf-8"))
    assert failure["case"] == "network-zone"
    assert failure["error"]["message"] == "Pricing tool did not complete: network-zone"
    assert failure["toolResult"] == ["Pricing execution timed out"]
    assert len(failure["checks"]) == 3
    assert failure["cleanupError"]["message"] == message
    cleanup.assert_awaited_once()


def test_success_does_not_write_failure_evidence(pricing_host, tmp_path):
    install, cleanup = pricing_host
    install(None, successful_queries=7)
    result = asyncio.run(pricing.check(IMAGE, PROXY, directory=tmp_path))
    assert len(result["checks"]) == 9
    assert result["livePricingVerified"] is True
    assert not (tmp_path / "failure.json").exists()
    cleanup.assert_awaited_once()


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
