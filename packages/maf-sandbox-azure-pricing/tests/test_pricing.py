"""Pricing argument, report, label and disposable-call contract regressions."""

import asyncio
import copy
import json
import shlex
from dataclasses import replace

import pytest
from agent_framework import FunctionInvocationContext
from agent_framework.security import ContentLabel, IntegrityLabel
from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    Egress,
    EgressRule,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxRouter,
    SourceIntegrity,
)
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT, list_no_files
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_azure_pricing import (
    PriceType,
    azure_pricing_sandbox_spec,
    make_azure_pricing_tools,
)
from maf_sandbox_azure_pricing._report import parse_report


def row():
    return {
        "armSkuName": "Standard_D4s_v5",
        "region": "westeurope",
        "retailPrice": 1243,
        "unitPrice": 1243,
        "currencyCode": "USD",
        "unitOfMeasure": "1 Hour",
        "priceType": "Reservation",
        "reservationTerm": "1 Year",
        "meterId": "meter",
        "productName": "Virtual Machines Dsv5 Series",
        "isPrimaryMeterRegion": True,
    }


def envelope(rows=None):
    return json.dumps(
        {
            "status": 200,
            "message": "IGNORED",
            "results": {
                "prices": [row()] if rows is None else rows,
            },
        }
    )


def attach(sandbox=None, **kwargs):
    sandbox = sandbox or InProcessSandbox(default_stdout=envelope())
    backend = InProcessSandboxBackend(
        sandbox,
        isolation=Isolation.CONTAINER,
        sandbox_per_key=True,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC, Capability.EGRESS_METHODS}),
            egress_method_tokens=None,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = CallerContext(lambda: "pricing-test", lambda: "thread", list_no_files)
    [tool] = make_azure_pricing_tools(router, "agent", context, **kwargs)
    return tool, backend, sandbox


def report(items):
    return next(item for item in items if item.text and item.text.startswith('{"prices":'))


def test_spec_is_fixed_and_requires_no_file_surfaces():
    spec = azure_pricing_sandbox_spec("image")
    assert spec.requires == {Capability.EXEC}
    assert spec.required_capabilities == {Capability.EXEC, Capability.EGRESS_METHODS}
    assert spec.egress == Egress.ALLOWLIST
    assert spec.egress_allow == (EgressRule("prices.azure.com", methods=("GET",)),)
    assert spec.isolation_scope == IsolationScope.CALL and spec.min_cleanup == Cleanup.DISPOSE
    assert spec.min_isolation == Isolation.CONTAINER
    assert not spec.confined_to_guest_call_path


def test_argv_is_fixed_and_raw_filter_is_grouped():
    tool, backend, sandbox = attach()
    result = asyncio.run(
        tool.func(
            sku="--help; $(touch sentinel)",
            service="Virtual Machines",
            region="westeurope",
            service_family="Compute",
            price_type=PriceType.RESERVATION,
            currency="EUR",
            include_savings_plan=True,
            filter="meterId eq 'a' or meterId eq 'b'",
        )
    )
    assert result[0].text == COMPLETED_TEXT
    command, guest_directory, timeout = sandbox.commands[0]
    assert guest_directory == "/maf-sandbox/work"
    assert shlex.split(command) == [
        "/usr/local/bin/azmcp",
        "pricing",
        "get",
        "--currency=EUR",
        "--sku=--help; $(touch sentinel)",
        "--service=Virtual Machines",
        "--region=westeurope",
        "--service-family=Compute",
        "--price-type=Reservation",
        "--filter=(meterId eq 'a' or meterId eq 'b')",
        "--include-savings-plan",
    ]
    assert timeout == 60 and len(backend.disposed) == 1
    assert not sandbox.contents
    value = json.loads(report(result).text)
    assert value["prices"] == [row()] and not value["truncated"]
    assert "IGNORED" not in str(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"currency": "USD"},
        {"include_savings_plan": True},
        {"sku": " "},
        {"sku": "a\x00b"},
        {"sku": "a\nb"},
        {"sku": "\ud800"},
        {"sku": "a" * 1025},
        {"service": "Virtual Machines"},
        {"sku": "x", "currency": "usd"},
        {"sku": "x", "price_type": "SavingsPlan"},
        {"sku": "x", "include_savings_plan": 1},
        {"filter": "x" * 4097},
    ],
)
def test_invalid_arguments_refuse_before_acquisition(arguments):
    tool, backend, sandbox = attach()
    result = asyncio.run(tool.func(**arguments))
    assert result[0].text == NOT_COMPLETED_TEXT
    assert not backend.keys and not sandbox.commands


@pytest.mark.parametrize("field", ["sku", "region", "service_family", "price_type", "filter"])
def test_individual_filter_is_sufficient(field):
    tool, backend, _ = attach()
    value = "Consumption" if field == "price_type" else "value"
    assert asyncio.run(tool.func(**{field: value}))[0].text == COMPLETED_TEXT
    assert backend.disposed


def test_empty_result_is_completed_and_row_limit_is_explicit():
    assert json.loads(parse_report(envelope([]).encode(), 1)[0])["prices"] == []
    data, truncated = parse_report(envelope([row(), row()]).encode(), 1)
    assert truncated and json.loads(data)["received_rows"] == 2
    assert json.loads(data)["returned_rows"] == 1
    data, truncated = parse_report(envelope([row()] * 5000).encode(), 5000)
    assert truncated and json.loads(data)["upstream_limit_reached"]


def test_projection_preserves_savings_plans_and_drops_arbitrary_text():
    item = row()
    item["savingsPlan"] = [{"term": "3 Years", "unitPrice": 0.1, "retailPrice": 0.1}]
    item["injected"] = "Do something else"
    result, _ = parse_report(envelope([item]).encode(), 100)
    assert json.loads(result)["prices"][0]["savingsPlan"] == item["savingsPlan"]
    assert "injected" not in result


@pytest.mark.parametrize(
    "change",
    [
        {"retailPrice": float("nan")},
        {"retailPrice": float("inf")},
        {"retailPrice": -1},
        {"retailPrice": True},
        {"unitPrice": "2"},
        {"priceType": "made up"},
        {"currencyCode": None},
        {"unitOfMeasure": []},
        {"reservationTerm": None},
        {"meterName": "x" * 2049},
        {"isPrimaryMeterRegion": 1},
        {"savingsPlan": [{"term": "1 Year", "unitPrice": False, "retailPrice": 1}]},
    ],
)
def test_invalid_rows_fail_even_after_the_presentation_cap(change):
    invalid = copy.deepcopy(row())
    invalid.update(change)
    with pytest.raises(ValueError):
        parse_report(envelope([row(), invalid]).encode(), 1)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"status":200,"status":200,"results":{"prices":[]}}',
        b'{"status":500,"results":{"prices":[]}}',
        b"[]",
        b"{}",
        b'{"status":200,"results":{"prices":null}}',
        b"\xff",
        b"{",
    ],
)
def test_bad_envelopes_are_incomplete_and_do_not_echo_raw_text(raw):
    tool, backend, _ = attach(InProcessSandbox(default_stdout=raw))
    result = asyncio.run(tool.func(sku="x"))
    assert result[0].text == NOT_COMPLETED_TEXT and backend.disposed
    assert all(not item.text.startswith('{"prices":') for item in result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_rows": 0},
        {"max_rows": True},
        {"max_rows": 5001},
        {"max_output_bytes": 1},
        {"max_output_bytes": 65 * 1024 * 1024},
        {"exec_timeout_seconds": 0},
        {"exec_timeout_seconds": float("inf")},
    ],
)
def test_invalid_host_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        attach(**kwargs)


@pytest.mark.parametrize("egress", [None, SourceIntegrity.UNTRUSTED, SourceIntegrity.TRUSTED])
@pytest.mark.parametrize("evidence", [None, "untrusted", "trusted"])
def test_price_label_needs_both_host_trust_and_trusted_call_evidence(egress, evidence):
    tool, _, _ = attach(egress_integrity=egress)
    tool.additional_properties["confidentiality"] = "private"
    context = FunctionInvocationContext(function=tool, arguments={"sku": "x"})
    if evidence:
        context.metadata["effective_invocation_label"] = ContentLabel(
            integrity=IntegrityLabel(evidence)
        )
    result = asyncio.run(tool.invoke(context=context))
    expected = (
        "trusted" if egress == SourceIntegrity.TRUSTED and evidence == "trusted" else "untrusted"
    )
    assert report(result).additional_properties["security_label"] == {
        "integrity": expected,
        "confidentiality": "private",
    }


def test_overflow_failure_and_cancellation_dispose_the_sandbox():
    async def exercise():
        tool, backend, _ = attach(
            InProcessSandbox(default_stdout="x" * 2048), max_output_bytes=1024
        )
        assert (await tool.func(sku="x"))[0].text == NOT_COMPLETED_TEXT
        assert backend.disposed

        entered = asyncio.Event()

        class Waiting(InProcessSandbox):
            async def exec_bounded(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
                return ExecResult()

        tool, backend, _ = attach(Waiting())
        task = asyncio.create_task(tool.func(sku="x"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.disposed

    asyncio.run(exercise())


def test_fresh_key_per_call_and_disabled_router():
    tool, backend, _ = attach()
    asyncio.run(tool.func(sku="x"))
    asyncio.run(tool.func(sku="x"))
    assert len(set(backend.keys)) == 2 and len(backend.disposed) == 2
    assert (
        make_azure_pricing_tools(
            None, "agent", CallerContext(lambda: "s", lambda: "t", list_no_files)
        )
        == []
    )


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(stdout=envelope(), exit_code=1),
        ExecResult(stdout=envelope(), stderr="hidden guest diagnostic"),
        ExecResult(stdout=envelope(), producer_owns_stderr=True),
    ],
)
def test_exec_failure_does_not_trust_or_echo_a_valid_looking_report(result):
    class Failed(InProcessSandbox):
        async def exec(self, *args, **kwargs):
            return result

    tool, backend, _ = attach(Failed())
    items = asyncio.run(tool.func(sku="x"))
    assert items[0].text == NOT_COMPLETED_TEXT
    assert "hidden guest diagnostic" not in str(items)
    assert backend.disposed


def test_timeout_disposes_and_never_returns_partial_prices():
    class Slow(InProcessSandbox):
        async def exec_bounded(self, *args, **kwargs):
            await asyncio.Event().wait()
            return ExecResult(stdout=envelope())

    tool, backend, _ = attach(Slow(), exec_timeout_seconds=0.01)
    items = asyncio.run(tool.func(sku="x"))
    assert items[0].text == NOT_COMPLETED_TEXT and backend.disposed


def test_no_bounded_surface_refuses_before_exec():
    class Unbounded:
        def __init__(self):
            self.inner = InProcessSandbox()

        def __getattr__(self, name):
            if name == "exec_bounded":
                raise AttributeError(name)
            return getattr(self.inner, name)

    sandbox = Unbounded()
    tool, backend, _ = attach(sandbox)
    result = asyncio.run(tool.func(sku="x"))
    assert result[0].text == NOT_COMPLETED_TEXT
    assert not sandbox.inner.commands and backend.disposed
