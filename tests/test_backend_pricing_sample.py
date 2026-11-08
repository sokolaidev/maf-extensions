"""Actual-result and cleanup evidence cannot be replaced with model-authored prose."""

import asyncio
import copy
import importlib.util
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
checker_spec = importlib.util.spec_from_file_location(
    "check_live_backend_pricing", ROOT / "scripts/check_live_backend_pricing.py"
)
assert checker_spec and checker_spec.loader
checker = importlib.util.module_from_spec(checker_spec)
checker_spec.loader.exec_module(checker)
PREFIX = checker.PREFIX
check = checker.check


def records():
    rows = [
        {
            "stage": "identity",
            "backend": "bubblewrap",
            "source": "a" * 40,
            "runtime": "sha256:" + "b" * 64,
            "proxy_sha256": "c" * 64,
            "versions": {
                "maf-sandbox": "0.53.0",
                "maf-sandbox-azure-pricing": "0.2.1",
                "maf-sandbox-bubblewrap": "0.4.2",
            },
            "source_sha256": {
                name: "d" * 64
                for name in (
                    "sample",
                    "maf_sandbox",
                    "maf_sandbox_azure_pricing",
                    "maf_sandbox_bubblewrap",
                )
            },
        }
    ]
    for index, category in enumerate(("Consumption", "Reservation")):
        call, instance = f"call-{index}", f"instance-{index}"
        price = {
            "retailPrice": 1.25,
            "currencyCode": "USD",
            "unitOfMeasure": "1 Hour",
            "priceType": category,
            "reservationTerm": "1 Year",
            "armSkuName": "Standard_D4s_v5",
            "region": "westeurope",
        }
        rows.extend(
            [
                {"stage": "acquire", "call": call, "instance": instance},
                {"stage": "call", "call": call, "seconds": 1.2, "unclean": 0, "failure": None},
                {
                    "stage": "report",
                    "call": call,
                    "seconds": 1.3,
                    "report": {"prices": [price], "truncated": False},
                },
                {"stage": "disposed", "instance": instance, "complete": True},
            ]
        )
    rows.append({"stage": "cleanup", "complete": True, "calls": 2, "model": False})
    return rows


def log(rows):
    return "\n".join(PREFIX + json.dumps(row) for row in rows)


def test_complete_tool_only_evidence_is_explicitly_not_model_qualification():
    assert check(log(records()), "bubblewrap", model=False)["calls"] == 2
    with pytest.raises(ValueError):
        check(log(records()), "bubblewrap")


def test_quoted_model_records_cannot_supply_host_evidence():
    with pytest.raises(ValueError):
        check("Model: " + json.dumps(log(records())), "bubblewrap", model=False)


def probe_records():
    names = [
        "method",
        "destination",
        "direct",
        "output",
        "timeout",
        "cancellation",
        "tool_timeout",
        "tool_cancellation",
    ]
    results = [{"name": name, "instance": name, "seconds": 1.0, "disposed": True} for name in names]
    for result in results[:2]:
        host = "prices.azure.com" if result["name"] == "method" else "example.com"
        method = "POST" if result["name"] == "method" else "GET"
        result["denial"] = {
            "source": "bubblewrap",
            "host": host,
            "method": method,
            "record": {
                "rejected_by": "allowlist",
                "audit": {"host": host, "method": method, "action": "reject", "status_code": 403},
            },
        }
    results[-2]["execution"] = {"started": True, "expired": True, "timeout_seconds": 0.001}
    return results


@pytest.mark.parametrize("missing", [False, True])
def test_qualification_requires_pricing_tool_failure_cleanup(missing):
    rows = records()
    results = probe_records()
    if missing:
        results.pop()
    rows.insert(1, {"stage": "probes", "results": results})
    if missing:
        with pytest.raises(ValueError, match="failure-cleanup"):
            check(log(rows), "bubblewrap", model=False, require_probes=True)
    else:
        check(log(rows), "bubblewrap", model=False, require_probes=True)


@pytest.mark.parametrize("failure", ["origin_403", "missing_denial", "no_expiry"])
def test_checker_requires_host_denial_and_deadline_evidence(failure):
    rows = records()
    results = probe_records()
    if failure == "origin_403":
        results[0]["denial"]["record"]["audit"]["action"] = "continue"
    elif failure == "missing_denial":
        del results[0]["denial"]
    else:
        results[-2]["execution"]["expired"] = False
    rows.insert(1, {"stage": "probes", "results": results})
    with pytest.raises(ValueError):
        check(log(rows), "bubblewrap", model=False, require_probes=True)


@pytest.mark.parametrize("failure", [None, "replacement", "different_name", "missing_mapping"])
def test_docker_policy_denial_is_bound_to_listed_instance(failure):
    rows = records()
    identity = rows[0]
    identity["backend"] = "docker-sbx"
    identity["cli"] = "sbx version v0.51.0"
    identity["versions"]["maf-sandbox-docker-sbx"] = "0.8.2"
    identity["source_sha256"]["maf_sandbox_docker_sbx"] = "e" * 64
    results = probe_records()
    for result in results[:2]:
        denial = result["denial"]
        name = "sandbox-" + result["name"]
        denial["source"] = "docker-sbx"
        denial["sandbox"] = {"id": result["instance"], "name": name}
        denial["record"] = {"sandbox_id": name, "host": denial["host"] + ":443", "count": 1}
    if failure == "replacement":
        results[0]["denial"]["sandbox"]["id"] = "different-instance"
    elif failure == "different_name":
        results[0]["denial"]["record"]["sandbox_id"] = "different-name"
    elif failure == "missing_mapping":
        del results[0]["denial"]["sandbox"]
    rows.insert(1, {"stage": "probes", "results": results})
    if failure is None:
        check(log(rows), "docker-sbx", model=False, require_probes=True)
    else:
        with pytest.raises(ValueError, match="sandbox proxy"):
            check(log(rows), "docker-sbx", model=False, require_probes=True)


@pytest.mark.parametrize(
    "failure",
    [
        "prose",
        "missing_cleanup",
        "missing_report",
        "reused_instance",
        "wrong_call",
        "unclean",
        "bad_time",
        "wrong_units",
        "wrong_sku",
        "missing_term",
        "truncated",
        "empty",
    ],
)
def test_evidence_refuses_incomplete_or_misattributed_results(failure):
    rows = copy.deepcopy(records())
    if failure == "prose":
        text = "The model says the price is 1.25 USD and cleanup succeeded."
    else:
        if failure == "missing_cleanup":
            rows.pop()
        elif failure == "missing_report":
            rows.pop(3)
        elif failure == "reused_instance":
            rows[5]["instance"] = rows[1]["instance"]
        elif failure == "wrong_call":
            rows[3]["call"] = "unrelated"
        elif failure == "unclean":
            rows[2]["unclean"] = 1
        elif failure == "bad_time":
            rows[2]["seconds"] = float("nan")
        elif failure == "wrong_units":
            rows[3]["report"]["prices"][0]["unitOfMeasure"] = ""
        elif failure == "wrong_sku":
            rows[3]["report"]["prices"][0]["armSkuName"] = "different"
        elif failure == "missing_term":
            del rows[7]["report"]["prices"][0]["reservationTerm"]
        elif failure == "truncated":
            rows[3]["report"]["truncated"] = True
        elif failure == "empty":
            rows[3]["report"]["prices"] = []
        text = log(rows)
    with pytest.raises(ValueError):
        check(text, "bubblewrap", model=False)


def load_sample(name="agent"):
    spec = importlib.util.spec_from_file_location(
        "backend_pricing_" + name, ROOT / "samples/experimental/azure_pricing" / (name + ".py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_log_name_requires_current_control_plane_identity():
    sample = load_sample()
    row = {"name": "sandbox", "id": "instance"}
    assert sample.control_identity([row], "sandbox", "instance") == row
    assert sample.control_identity({"sandboxes": [row]}, "sandbox", "instance") == row
    for rows in ([], [dict(row, id="replacement")], [row, row]):
        with pytest.raises(ValueError, match="acquired sandbox"):
            sample.control_identity(rows, "sandbox", "instance")


def test_origin_403_cannot_supply_proxy_denial():
    qualification = load_sample("qualification")
    record = probe_records()[0]["denial"]["record"]
    assert (
        qualification.denial_record("bubblewrap", [record], "instance", "prices.azure.com", "POST")
        == record
    )
    record["audit"]["action"] = "continue"
    assert (
        qualification.denial_record("bubblewrap", [record], "instance", "prices.azure.com", "POST")
        is None
    )
    blocked = {"sandbox_id": "instance", "host": "prices.azure.com", "count": 1}
    assert (
        qualification.denial_record(
            "docker-sbx", {"allowed": [blocked]}, "instance", "prices.azure.com", "POST"
        )
        is None
    )
    assert (
        qualification.denial_record(
            "docker-sbx", {"blocked": [blocked]}, "different", "prices.azure.com", "POST"
        )
        is None
    )
    assert (
        qualification.denial_record(
            "docker-sbx", {"blocked": [blocked]}, "instance", "prices.azure.com", "POST"
        )
        == blocked
    )


@pytest.mark.parametrize(
    "failure", [ValueError("invalid report"), TimeoutError("unrelated timeout")]
)
def test_generic_execution_failure_cannot_supply_deadline_evidence(failure):
    qualification = load_sample("qualification")

    async def scenario():
        sandbox = Mock(exec_bounded=AsyncMock(side_effect=failure))
        probe = qualification.DeadlineProbe(sandbox)
        with pytest.raises(type(failure)):
            await probe.exec_bounded(
                ["azmcp"], working_directory=".", timeout=90, max_output_bytes=4096
            )
        sandbox.exec_bounded.assert_awaited_once()
        with pytest.raises(ValueError, match="forced deadline"):
            probe.evidence()

    asyncio.run(scenario())


def test_execution_deadline_evidence_requires_expiry_and_awaits_cleanup(monkeypatch):
    qualification = load_sample("qualification")
    monkeypatch.setattr(qualification.DeadlineProbe, "seconds", 0)

    async def scenario():
        cleaned = False

        async def execute(*_args, **_kwargs):
            nonlocal cleaned
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned = True

        sandbox = Mock(exec_bounded=AsyncMock(side_effect=execute))
        probe = qualification.DeadlineProbe(sandbox)
        with pytest.raises(TimeoutError):
            await probe.exec_bounded(
                ["azmcp"], working_directory=".", timeout=90, max_output_bytes=4096
            )
        assert cleaned and probe.evidence()["expired"] is True

    asyncio.run(scenario())


def test_sample_preserves_empty_and_truncated_tool_reports():
    from agent_framework import Content
    from maf_sandbox.maf import COMPLETED_TEXT

    sample = load_sample()
    value = {"prices": [], "truncated": True}
    answer = [Content.from_text(COMPLETED_TEXT), Content.from_text(json.dumps(value))]
    assert sample.report(answer) == value
    with pytest.raises(ValueError, match="did not complete"):
        sample.report([Content.from_text("The model claims there are prices")])


def test_unfiltered_report_accepts_mixed_prices_but_explicit_filter_is_enforced():
    from agent_framework import Content
    from maf_sandbox.maf import COMPLETED_TEXT

    sample = load_sample()
    value = copy.deepcopy(records()[3]["report"])
    value["prices"] = [
        dict(value["prices"][0], priceType=category)
        for category in ("Consumption", "Reservation", "DevTestConsumption")
    ]
    answer = [Content.from_text(COMPLETED_TEXT), Content.from_text(json.dumps(value))]
    assert sample.report(answer) == value
    assert sample.report(answer, None) == value
    with pytest.raises(ValueError, match="price type"):
        sample.report(answer, "Consumption")


def test_evidence_accepts_dev_test_prices_without_losing_required_coverage():
    rows = records()
    prices = rows[3]["report"]["prices"]
    prices.append(dict(prices[0], priceType="DevTestConsumption"))
    check(log(rows), "bubblewrap", model=False)
    prices[0]["priceType"] = "DevTestConsumption"
    with pytest.raises(ValueError, match="coverage"):
        check(log(rows), "bubblewrap", model=False)
    prices[0]["priceType"] = "unknown"
    with pytest.raises(ValueError, match="price type"):
        check(log(rows), "bubblewrap", model=False)


def test_model_calls_require_explicit_opt_in():
    sample = load_sample()
    args = ["--backend", "docker-sbx", "--image", "template", "--source", "source"]
    assert sample.parser().parse_args(args).no_model is True
    assert sample.parser().parse_args([*args, "--no-model"]).no_model is True
    assert sample.parser().parse_args([*args, "--model"]).no_model is False
    with pytest.raises(SystemExit):
        sample.parser().parse_args([*args, "--model", "--no-model"])


def test_model_budget_serializes_calls_through_evidence_checks():
    sample = load_sample()

    async def scenario():
        order = []

        async def checked(answer, category, seconds):
            assert answer == "report" and category is None and seconds > 0
            order.append("check")
            await asyncio.sleep(0)
            order.append("verified")

        async def dispatch():
            order.append("run")
            await asyncio.sleep(0)

        guard = sample.ModelCalls(checked)
        context = Mock(arguments={}, result="report")
        results = await asyncio.gather(
            *(guard.process(context, dispatch) for _ in range(3)), return_exceptions=True
        )
        assert results[:2] == [None, None]
        assert isinstance(results[2], ValueError) and "budget" in str(results[2])
        assert order == ["run", "check", "verified"] * 2

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [{}, {"price_type": None}, {"price_type": "Reservation"}])
def test_model_price_filter_preserves_missing_and_null(arguments):
    sample = load_sample()

    async def scenario():
        checked = AsyncMock()
        context = Mock(arguments=arguments, result="report")
        await sample.ModelCalls(checked).process(context, AsyncMock())
        answer, category, seconds = checked.call_args.args
        assert answer == "report" and category == arguments.get("price_type") and seconds > 0

    asyncio.run(scenario())


def test_failed_model_calls_consume_the_budget():
    sample = load_sample()

    async def scenario():
        checked = AsyncMock()
        dispatch = AsyncMock(side_effect=RuntimeError("tool failed"))
        guard = sample.ModelCalls(checked)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="tool failed"):
                await guard.process(Mock(arguments={}), dispatch)
        with pytest.raises(ValueError, match="budget"):
            await guard.process(Mock(arguments={}), dispatch)
        assert dispatch.await_count == 2
        checked.assert_not_awaited()

    asyncio.run(scenario())


def test_model_deadline_cancels_work_and_awaits_cleanup(monkeypatch):
    sample = load_sample()
    monkeypatch.setattr(sample, "MODEL_TIMEOUT_SECONDS", 0)

    async def scenario():
        cleaned = False

        async def blocked_run(_message):
            nonlocal cleaned
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned = True

        agent = Mock(run=AsyncMock(side_effect=blocked_run))
        with pytest.raises(TimeoutError):
            await sample.model_response(agent)
        agent.run.assert_awaited_once()
        assert cleaned

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [b"", b"HTTP/1.1 200 OK\r\n\r\n", OSError("peer reset after request")],
)
def test_direct_probe_does_not_accept_ambiguous_responses(monkeypatch, response):
    connection = MagicMock()
    tls = MagicMock()
    if isinstance(response, OSError):
        tls.recv.side_effect = response
    else:
        tls.recv.return_value = response
    context = MagicMock()
    context.wrap_socket.return_value.__enter__.return_value = tls
    monkeypatch.setattr("socket.create_connection", Mock(return_value=connection))
    monkeypatch.setattr("ssl.create_default_context", Mock(return_value=context))
    with pytest.raises((RuntimeError, OSError)):
        run_direct_probe()
    tls.sendall.assert_called_once()


def run_direct_probe():
    """Execute the checked-in probe with test-installed network fakes."""
    exec(load_sample("qualification").DIRECT_PROBE, {})  # nosec B102


@pytest.mark.parametrize("stage", ["connect", "handshake", "certificate"])
def test_direct_probe_distinguishes_setup_refusal_from_bad_trust(monkeypatch, capsys, stage):
    connection = MagicMock()
    connect = Mock(return_value=connection)
    context = MagicMock()
    if stage == "connect":
        connect.side_effect = ConnectionRefusedError("denied")
    elif stage == "handshake":
        context.wrap_socket.side_effect = ssl.SSLEOFError("denied before TLS is established")
    else:
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("untrusted peer")
    monkeypatch.setattr("socket.create_connection", connect)
    monkeypatch.setattr("ssl.create_default_context", Mock(return_value=context))
    if stage == "certificate":
        with pytest.raises(ssl.SSLCertVerificationError):
            run_direct_probe()
    else:
        run_direct_probe()
        assert capsys.readouterr().out == "blocked\n"


def test_live_deadline_retains_partial_evidence(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.log"
    monkeypatch.setenv("MAF_PRICING_LIVE_ARGS", '["--backend", "bubblewrap"]')
    monkeypatch.setenv("MAF_PRICING_EVIDENCE_LOG", str(evidence))

    def timeout(command, *, stdout, stderr, timeout):
        assert "-u" in command and stderr == subprocess.STDOUT
        stdout.write(b"partial host report\ncleanup diagnostic\n")
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        test_live_backend_pricing()
    assert evidence.read_bytes() == b"partial host report\ncleanup diagnostic\n"


@pytest.mark.skipif(os.environ.get("MAF_PRICING_E2E") != "1", reason="opt-in real pricing host")
def test_live_backend_pricing():
    args = json.loads(os.environ["MAF_PRICING_LIVE_ARGS"])
    assert isinstance(args, list) and all(isinstance(item, str) for item in args)
    backend = args[args.index("--backend") + 1]
    evidence = Path(os.environ["MAF_PRICING_EVIDENCE_LOG"])
    with evidence.open("wb") as output:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(ROOT / "samples/experimental/azure_pricing/agent.py"),
                *args,
                "--no-model",
                "--qualify",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    text = evidence.read_text("utf-8", errors="replace")
    assert result.returncode == 0, text[-8192:]
    check(text, backend, model=False, require_probes=True)
