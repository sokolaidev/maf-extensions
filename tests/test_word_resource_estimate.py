"""Admission ordering, inventory accounting and exact retail cost calculations."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/23_word_resource_estimate"


@pytest.fixture(scope="module")
def sample():
    before = {
        name: sys.modules.get(name)
        for name in (
            "_scaffold",
            "estimate",
            "make_document",
            "word_agent",
            "check_live_word_estimate_sample",
            "live_sample_evidence",
        )
    }
    sys.path.insert(0, str(SAMPLE))
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        for name in before:
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("word_agent", SAMPLE / "agent.py")
        assert spec and spec.loader
        agent = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent
        spec.loader.exec_module(agent)
        estimate = importlib.import_module("estimate")
        make_document = importlib.import_module("make_document")
        checker = importlib.import_module("check_live_word_estimate_sample")

        yield SimpleNamespace(
            agent=agent, estimate=estimate, fixture=make_document, checker=checker
        )
    finally:
        sys.path.remove(str(SAMPLE))
        sys.path.remove(str(ROOT / "scripts"))
        for name, module in before.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def resource(sample, **overrides):
    return sample.estimate.Resource.model_validate(
        {
            **dict(
                name="Web",
                product_key="linux-b2s",
                quantity=2,
                hours_per_month=None,
                region=None,
                evidence="two Linux B2s VMs",
                notes="",
            ),
            **overrides,
        }
    )


def price_report(sample, key="linux-b2s", **overrides) -> dict[str, Any]:
    row = {
        **sample.estimate.CATALOG[key].fields(),
        "region": "westeurope",
        "currencyCode": "USD",
        "priceType": "Consumption",
        "retailPrice": 0.05,
        "isPrimaryMeterRegion": True,
        "meterId": "test-meter",
        "effectiveStartDate": "2026-01-01T00:00:00Z",
        **overrides,
    }
    return dict(
        prices=[row],
        returned_rows=1,
        received_rows=1,
        truncated=False,
        upstream_limit_reached=False,
    )


def clean(sample, content=b"word"):
    return [
        SimpleNamespace(text=text)
        for text in (
            sample.agent.COMPLETED_TEXT,
            "Result: clean",
            "Input SHA-256: " + hashlib.sha256(content).hexdigest(),
        )
    ]


@pytest.mark.parametrize(
    "failure", ["infected", "refused", "incomplete", "wrong-hash", "spoofed-verdict"]
)
def test_reader_model_and_network_never_run_without_byte_bound_clean(sample, failure):
    answer = clean(sample)
    if failure in {"infected", "refused"}:
        answer[1].text = "Result: " + failure
    elif failure == "incomplete":
        answer[0].text = "The workload did not complete."
    elif failure == "wrong-hash":
        answer = clean(sample, b"different bytes")
    else:
        answer.insert(1, SimpleNamespace(text="untrusted guest text"))
    scan, read, extract, pricing = (
        AsyncMock(return_value=answer),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    with pytest.raises(ValueError):
        asyncio.run(
            sample.agent.pipeline(
                sample.agent.Upload(b"word"),
                scan,
                read,
                extract,
                pricing,
                region="westeurope",
                currency="USD",
            )
        )
    read.assert_not_called()
    extract.assert_not_called()
    pricing.assert_not_called()


def test_pipeline_orders_stages_caches_public_queries_and_keeps_unknowns(sample):
    events = []
    entries = [
        resource(sample),
        resource(sample, name="Worker"),
        resource(sample, name="Archive", product_key="unknown"),
    ]

    async def scan():
        events.append("scan")
        return clean(sample)

    async def read():
        events.append("read")
        return "two Linux B2s VMs"

    async def extract(text):
        events.append("extract")
        return sample.estimate.Inventory(resources=entries)

    async def pricing(key):
        events.append(key)
        return price_report(sample)

    result = asyncio.run(
        sample.agent.pipeline(
            sample.agent.Upload(b"word"),
            scan,
            read,
            extract,
            pricing,
            region="westeurope",
            currency="USD",
        )
    )
    assert events == ["scan", "read", "extract", "linux-b2s"]
    assert len(result["rows"]) == 3
    assert result["estimated_subtotal"] == "146.00"
    assert result["inventory_total"] is None and result["unresolved_resources"] == 1
    assert result["rows"][2]["monthly_cost"] is None
    assert result["pricing_failures"] == {}


@pytest.mark.parametrize(
    "failure",
    [
        "lookup",
        "invalid-json",
        "tool-incomplete",
        "incomplete",
        "wrong-meter",
        "empty",
        "duplicate",
    ],
)
def test_failed_product_lookup_is_not_retried_for_each_resource(sample, failure, tmp_path, capsys):
    keys = list(sample.estimate.CATALOG)
    entries = [resource(sample, product_key=keys[index % 3]) for index in range(49)]
    entries.append(resource(sample, product_key=keys[3]))
    calls = []
    responses = {}

    async def pricing(key):
        calls.append(key)
        if key == keys[3]:
            return price_report(sample, key)
        if failure == "lookup":
            raise ValueError("Pricing lookup incomplete")
        if failure in {"invalid-json", "tool-incomplete"}:
            texts = (
                [sample.agent.COMPLETED_TEXT, '{"prices":invalid']
                if failure == "invalid-json"
                else ["The workload did not complete.", "Pricing lookup incomplete: CLI failed."]
            )
            responses[key] = texts
            return sample.agent.pricing_report([SimpleNamespace(text=text) for text in texts])
        report = price_report(sample, key)
        if failure == "incomplete":
            report["truncated"] = True
        elif failure == "wrong-meter":
            report["prices"][0]["meterName"] = "wrong meter"
        elif failure == "empty":
            report.update(prices=[], returned_rows=0, received_rows=0)
        elif failure == "duplicate":
            report.update(prices=report["prices"] * 2, returned_rows=2, received_rows=2)
        responses[key] = report
        return report

    report = asyncio.run(
        sample.agent.pipeline(
            sample.agent.Upload(b"word"),
            AsyncMock(return_value=clean(sample)),
            AsyncMock(return_value=entries[0].evidence),
            AsyncMock(return_value=sample.estimate.Inventory(resources=entries)),
            pricing,
            region="westeurope",
            currency="USD",
        )
    )
    assert calls == keys
    assert len(report["rows"]) == 50
    assert report["unresolved_resources"] == 49
    assert report["inventory_total"] is None
    assert all(row["price"] is None for row in report["rows"][:-1])
    assert report["rows"][-1]["monthly_cost"] == "0.10"
    assert report["estimated_subtotal"] == "0.10"
    failures = report["pricing_failures"]
    assert set(failures) == set(keys[:3])
    for key, diagnostic in failures.items():
        assert diagnostic["query"] == sample.estimate.CATALOG[key].query()
        assert diagnostic["region"] == "westeurope"
        assert diagnostic["currency"] == "USD"
        assert diagnostic["price_type"] == "Consumption"
        assert diagnostic["error"]["truncated"] is False
        assert diagnostic["response"]["truncated"] is False
        assert json.loads(diagnostic["response"]["text"]) == responses.get(key)
        expected_stage, expected_error = {
            "lookup": ("lookup", "Pricing lookup incomplete"),
            "invalid-json": ("tool_result", "Expecting value"),
            "tool-incomplete": ("tool_result", "Tool did not complete"),
            "incomplete": ("selection", "Incomplete pricing response"),
            "wrong-meter": ("selection", "0 matches in 1 rows"),
            "empty": ("selection", "0 matches in 0 rows"),
            "duplicate": ("selection", "2 matches in 2 rows"),
        }[failure]
        assert diagnostic["stage"] == expected_stage
        assert expected_error in json.loads(diagnostic["error"]["text"])
        assert diagnostic["error_type"] == (
            "JSONDecodeError" if failure == "invalid-json" else "ValueError"
        )
    output = tmp_path / "report"
    sample.agent.save_report(output, report)
    saved = json.loads((output / "estimate.json").read_text(encoding="utf-8"))
    assert saved["pricing_failures"] == failures
    captured = capsys.readouterr()
    assert "Pricing lookup" not in captured.out + captured.err
    assert "wrong meter" not in captured.out + captured.err


@pytest.mark.parametrize("case", ["success", "missing", "duplicate", "unlabelled", "non-text"])
def test_pricing_report_retains_rejected_tool_text(sample, case):
    report = price_report(sample)
    texts = [sample.agent.COMPLETED_TEXT, json.dumps(report)]
    if case == "missing":
        texts.pop()
    elif case == "duplicate":
        texts.append(texts[-1])
    answer = [SimpleNamespace(text=text) for text in texts] if case != "unlabelled" else None
    if case == "non-text":
        answer = [SimpleNamespace(text=object())]
    if case == "success":
        assert sample.agent.pricing_report(answer) == report
    else:
        with pytest.raises(sample.agent.PricingResponseError) as error:
            sample.agent.pricing_report(answer)
        expected = {"unlabelled": {"type": "NoneType"}, "non-text": [None]}.get(case, texts)
        assert error.value.response == expected


def test_pricing_diagnostic_snapshots_are_bounded_and_mark_truncation(sample):
    limit = sample.agent.MAX_DIAGNOSTIC_CHARS
    exact = "x" * (limit - 2)
    assert sample.agent.diagnostic_snapshot(exact) == {
        "text": json.dumps(exact),
        "truncated": False,
    }
    oversized = {"message": "\u2603\n" * limit}
    snapshot = sample.agent.diagnostic_snapshot(oversized)
    assert snapshot == {"text": json.dumps(oversized)[:limit], "truncated": True}
    assert len(snapshot["text"]) == limit


@pytest.mark.parametrize("error", [RuntimeError("cleanup failed"), asyncio.CancelledError()])
def test_fatal_pricing_failure_is_not_cached_or_swallowed(sample, error):
    pricing = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(
            sample.agent.pipeline(
                sample.agent.Upload(b"word"),
                AsyncMock(return_value=clean(sample)),
                AsyncMock(return_value="two Linux B2s VMs"),
                AsyncMock(return_value=sample.estimate.Inventory(resources=[resource(sample)] * 2)),
                pricing,
                region="westeurope",
                currency="USD",
            )
        )
    pricing.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides", [dict(quantity=None), dict(region="eastus"), dict(product_key="unknown")]
)
def test_incomplete_inventory_does_not_query_pricing(sample, overrides):
    entry = resource(sample, **overrides)
    pricing = AsyncMock()
    report = asyncio.run(
        sample.agent.pipeline(
            sample.agent.Upload(b"word"),
            AsyncMock(return_value=clean(sample)),
            AsyncMock(return_value=entry.evidence),
            AsyncMock(return_value=sample.estimate.Inventory(resources=[entry])),
            pricing,
            region="westeurope",
            currency="USD",
        )
    )
    pricing.assert_not_called()
    assert report["inventory_total"] is None


def test_unquoted_inventory_evidence_is_rejected_before_pricing(sample):
    pricing = AsyncMock()
    with pytest.raises(ValueError, match="evidence"):
        asyncio.run(
            sample.agent.pipeline(
                sample.agent.Upload(b"word"),
                AsyncMock(return_value=clean(sample)),
                AsyncMock(return_value="unrelated text"),
                AsyncMock(return_value=sample.estimate.Inventory(resources=[resource(sample)])),
                pricing,
                region="westeurope",
                currency="USD",
            )
        )
    pricing.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("priceType", "Reservation"),
        ("priceType", "DevTestConsumption"),
        ("currencyCode", "EUR"),
        ("unitOfMeasure", "100 Hours"),
        ("region", "eastus"),
        ("skuName", "B2s Spot"),
        ("productName", "Virtual Machines BS Series Windows"),
        ("isPrimaryMeterRegion", False),
        ("effectiveStartDate", "2999-01-01T00:00:00Z"),
        ("effectiveStartDate", "invalid"),
        ("effectiveStartDate", "2026-01-01"),
        ("meterId", ""),
        ("reservationTerm", "1 Year"),
        ("retailPrice", float("nan")),
        ("retailPrice", -1),
        ("retailPrice", True),
    ],
)
def test_wrong_meter_never_becomes_an_estimate(sample, field, value):
    with pytest.raises(ValueError):
        sample.estimate.select_price(
            price_report(sample, **{field: value}),
            sample.estimate.CATALOG["linux-b2s"],
            "westeurope",
            "USD",
        )


@pytest.mark.parametrize(
    "case", ["empty", "ambiguous", "truncated", "upstream-limit", "count-mismatch"]
)
def test_incomplete_or_ambiguous_prices_are_not_summed_or_chosen(sample, case):
    report = price_report(sample)
    if case == "empty":
        report.update(prices=[], received_rows=0, returned_rows=0)
    elif case == "ambiguous":
        report.update(prices=report["prices"] * 2, received_rows=2, returned_rows=2)
    elif case == "count-mismatch":
        report["received_rows"] = 2
    else:
        report["truncated" if case == "truncated" else "upstream_limit_reached"] = True
    with pytest.raises(ValueError):
        sample.estimate.select_price(
            report, sample.estimate.CATALOG["linux-b2s"], "westeurope", "USD"
        )


def test_region_selected_disk_price_remains_complete(sample):
    report = price_report(sample, "premium-ssd-p10", retailPrice=21.68)
    report.update(received_rows=2, selected_rows=1, requested_region="westeurope")
    price = sample.estimate.select_price(
        report, sample.estimate.CATALOG["premium-ssd-p10"], "westeurope", "USD"
    )
    row = sample.estimate.estimate_row(resource(sample, product_key="premium-ssd-p10"), price, None)
    assert row["monthly_cost"] == "43.36"
    assert row["status"] == "Estimated"


@pytest.mark.parametrize(
    "overrides",
    [
        {"received_rows": 0},
        {"received_rows": True},
        {"received_rows": "2"},
        {"returned_rows": 0},
        {"returned_rows": True},
        {"selected_rows": 2},
        {"selected_rows": None},
        {"selected_rows": True},
        {"truncated": True},
        {"upstream_limit_reached": True},
    ],
)
def test_region_selection_does_not_hide_incomplete_prices(sample, overrides):
    report = price_report(sample)
    report.update(received_rows=2, selected_rows=1, requested_region="westeurope")
    report.update(overrides)
    with pytest.raises(ValueError, match="Incomplete pricing response"):
        sample.estimate.select_price(
            report, sample.estimate.CATALOG["linux-b2s"], "westeurope", "USD"
        )


def test_hourly_and_monthly_units_and_zero_usage(sample):
    hourly = price_report(sample)["prices"][0]
    assert sample.estimate.estimate_row(resource(sample), hourly, None)["monthly_cost"] == "73.00"
    assert (
        sample.estimate.estimate_row(resource(sample, hours_per_month=160.0), hourly, None)[
            "monthly_cost"
        ]
        == "16.00"
    )
    assert (
        sample.estimate.estimate_row(resource(sample, hours_per_month=0.0), hourly, None)[
            "monthly_cost"
        ]
        == "0.00"
    )
    disk = price_report(sample, "premium-ssd-p10", retailPrice=19.71)["prices"][0]
    assert (
        sample.estimate.estimate_row(resource(sample, product_key="premium-ssd-p10"), disk, None)[
            "monthly_cost"
        ]
        == "39.42"
    )


def test_output_preserves_existing_files_and_escapes_document_markup(sample, tmp_path):
    row = sample.estimate.estimate_row(
        resource(sample, name="<script> | [link](https://example.com)"), None, "Unknown"
    )
    report = sample.estimate.report_document([row], "westeurope", "USD")
    output = tmp_path / "report"
    sample.agent.save_report(output, report)
    rendered = (output / "estimate.md").read_text(encoding="utf-8")
    assert "<script>" not in rendered and "\\|" in rendered and "\\[link\\]" in rendered
    assert "No complete inventory total" in rendered
    with pytest.raises(FileExistsError):
        sample.agent.save_report(output, report)
    assert json.loads((output / "estimate.json").read_text()) == report


def test_failed_output_write_removes_only_owned_files(sample, tmp_path, monkeypatch):
    original = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: b"mismatch" if path.name == "estimate.md" else original(path),
    )
    output = tmp_path / "report"
    with pytest.raises(RuntimeError, match="read-back"):
        sample.agent.save_report(output, sample.estimate.report_document([], "westeurope", "USD"))
    assert not output.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@pytest.mark.parametrize("mask", [0, 0o022])
def test_report_permissions_are_owner_only(sample, tmp_path, mask):
    previous = os.umask(mask)
    output = tmp_path / "report"
    try:
        sample.agent.save_report(output, sample.estimate.report_document([], "westeurope", "USD"))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in ("estimate.json", "estimate.md"):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600


def test_input_is_a_bounded_immutable_snapshot(sample, tmp_path, monkeypatch):
    path = tmp_path / "input.docx"
    path.write_bytes(b"word")
    upload = sample.agent.load_upload(path)
    path.write_bytes(b"changed")
    assert asyncio.run(upload.read("incoming.docx")) == b"word"
    assert asyncio.run(upload.read(str(path))) is None
    monkeypatch.setattr(sample.agent, "MAX_INPUT", 2)
    with pytest.raises(ValueError, match="16 MiB"):
        sample.agent.load_upload(path)


def test_fixture_converts_through_real_guarded_guest(sample, tmp_path):
    from importlib.resources import files

    (tmp_path / "input.bin").write_bytes(sample.fixture.document_bytes())
    config = dict(
        kind="docx",
        format="text",
        pages=None,
        max_pages=200,
        max_chars=30000,
        image_dpi=96,
        max_image_pixels=16000000,
        max_image_pages=20,
    )
    (tmp_path / "request.json").write_text(json.dumps(config))
    for name in ("_guest.py", "_pdf_guard.py"):
        (tmp_path / name).write_bytes(files("maf_sandbox_document").joinpath(name).read_bytes())
    completed = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "_guest.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    text = (tmp_path / "document.txt").read_text(encoding="utf-8")
    assert all(paragraph in text for paragraph in sample.fixture.PARAGRAPHS)


def test_scaffold_is_canonical(sample):
    assert (SAMPLE / "_scaffold.py").read_bytes() == (
        ROOT / "samples/01_acas_bicep/_scaffold.py"
    ).read_bytes()


@pytest.mark.parametrize("creator_system", [0, 3], ids=["windows", "unix"])
def test_checked_in_fixture_and_inventory_agree(sample, monkeypatch, creator_system):
    zip_info = sample.fixture.zipfile.ZipInfo
    original_init = zip_info.__init__

    def platform_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.create_system = creator_system

    monkeypatch.setattr(zip_info, "__init__", platform_init)
    assert (SAMPLE / "resources.docx").read_bytes() == sample.fixture.document_bytes()
    inventory = sample.estimate.Inventory.model_validate(sample.fixture.inventory_data())
    sample.estimate.validate_inventory(inventory, "\n".join(sample.fixture.PARAGRAPHS))
    assert len(inventory.resources) == 4


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_and_cancellation_purge_scope_without_report(sample, tmp_path, monkeypatch, cancel):
    agent = sample.agent
    path = tmp_path / "input.docx"
    path.write_bytes(sample.fixture.document_bytes())
    monkeypatch.setattr(agent, "require_env_vars", lambda names: dict.fromkeys(names, "configured"))
    monkeypatch.setattr(
        agent.DockerSandboxBackend,
        "create",
        AsyncMock(return_value=SimpleNamespace(isolation="container")),
    )
    router = SimpleNamespace(dispose_scope=AsyncMock(return_value=SimpleNamespace(undisposed=0)))
    monkeypatch.setattr(agent, "SandboxRouter", lambda *args, **kwargs: router)
    error = asyncio.CancelledError() if cancel else RuntimeError("scanner failed")
    scanner = SimpleNamespace(func=AsyncMock(side_effect=error))
    reader = SimpleNamespace(func=AsyncMock())
    pricer = SimpleNamespace(func=AsyncMock())
    monkeypatch.setattr(agent, "make_antivirus_tools", lambda *args, **kwargs: [scanner])
    monkeypatch.setattr(agent, "make_document_tools", lambda *args, **kwargs: [reader])
    monkeypatch.setattr(agent, "make_azure_pricing_tools", lambda *args, **kwargs: [pricer])
    with pytest.raises(type(error)):
        asyncio.run(agent.run(path, tmp_path / "out", "westeurope", "USD", None, True))
    router.dispose_scope.assert_awaited_once()
    reader.func.assert_not_called()
    pricer.func.assert_not_called()
    assert not (tmp_path / "out").exists()


def test_success_retains_document_content_only_in_report_files(
    sample, tmp_path, monkeypatch, capsys
):
    agent = sample.agent
    path = tmp_path / "input.docx"
    path.write_bytes(sample.fixture.document_bytes())
    monkeypatch.setattr(agent, "require_env_vars", lambda names: dict.fromkeys(names, "configured"))
    monkeypatch.setattr(
        agent.DockerSandboxBackend,
        "create",
        AsyncMock(return_value=SimpleNamespace(isolation="container")),
    )
    router = SimpleNamespace(dispose_scope=AsyncMock(return_value=SimpleNamespace(undisposed=0)))
    monkeypatch.setattr(agent, "SandboxRouter", lambda *args, **kwargs: router)
    for factory in ("make_antivirus_tools", "make_document_tools", "make_azure_pricing_tools"):
        monkeypatch.setattr(agent, factory, lambda *args, **kwargs: [SimpleNamespace()])
    entry = resource(
        sample, name="private-name", notes="private-notes", evidence="private-evidence"
    )
    row = sample.estimate.estimate_row(entry, None, "Unknown")
    report = sample.estimate.report_document([row], "westeurope", "USD")
    monkeypatch.setattr(agent, "pipeline", AsyncMock(return_value=report))
    output = tmp_path / "out"
    assert asyncio.run(agent.run(path, output, "westeurope", "USD", None, True)) == 0
    captured = capsys.readouterr()
    saved = (output / "estimate.json").read_text(encoding="utf-8")
    for value in (entry.name, entry.notes, entry.evidence):
        assert value in saved
        assert value not in captured.out + captured.err
    assert "private-name" in (output / "estimate.md").read_text(encoding="utf-8")
    assert '"stage": "artifact"' in captured.out
    assert '"stage": "report"' in captured.out


def test_fixture_mode_never_substitutes_inventory_for_another_document(
    sample, tmp_path, monkeypatch
):
    path = tmp_path / "input.docx"
    path.write_bytes(b"another document")
    monkeypatch.setattr(
        sample.agent, "require_env_vars", lambda names: dict.fromkeys(names, "configured")
    )
    backend = AsyncMock()
    monkeypatch.setattr(sample.agent.DockerSandboxBackend, "create", backend)
    with pytest.raises(ValueError, match="exact generated"):
        asyncio.run(sample.agent.run(path, tmp_path / "out", "westeurope", "USD", None, True))
    backend.assert_not_called()


def test_cleanup_observer_rejects_missing_and_failed_disposal(sample):
    observer = sample.agent.Cleanup()
    call = SimpleNamespace(call="call", keys=["key"], failure=None, unclean=0, seconds=0.1)
    observer.tool_call_ended(call)
    assert observer.calls == [False]
    disposal = SimpleNamespace(call="call", key="key", outcome="gone", failure=None)
    observer.sandbox_disposed(disposal)
    observer.tool_call_ended(call)
    assert observer.calls[-1] is True
    disposal.failure = "failed"
    observer.tool_call_ended(call)
    assert observer.calls[-1] is False


def live_evidence(sample):
    inventory = sample.estimate.Inventory.model_validate(sample.fixture.inventory_data())
    rows = [
        sample.estimate.estimate_row(
            entry, price_report(sample, entry.product_key)["prices"][0], None
        )
        if entry.product_key != "unknown"
        else sample.estimate.estimate_row(entry, None, "Unknown")
        for entry in inventory.resources
    ]
    report = sample.estimate.report_document(rows, "westeurope", "USD")
    report["inventory_source"] = "model"
    artifacts = {
        "estimate.json": json.dumps(report).encode(),
        "estimate.md": sample.estimate.markdown(report).encode(),
    }
    calls = [dict(stage="tool_call", call=str(i), complete=True, seconds=0.1) for i in range(5)]
    measured = [
        calls[0],
        dict(stage="scan", clean=True, byte_bound=True),
        calls[1],
        dict(stage="inventory", resources=4),
        *calls[2:],
        dict(stage="cleanup", complete=True, calls=5),
    ]
    measured += [
        dict(
            stage="artifact", name=name, sha256=hashlib.sha256(value).hexdigest(), bytes=len(value)
        )
        for name, value in artifacts.items()
    ]
    measured.append(dict(stage="report", unresolved=1, files=2))
    return measured, artifacts


def evidence_log(measured):
    return "\n".join("  [measured] " + json.dumps(item) for item in measured)


def retain_live_report(sample, measured, artifacts, report):
    artifacts["estimate.json"] = json.dumps(report).encode()
    artifacts["estimate.md"] = sample.estimate.markdown(report).encode()
    for event in measured[8:10]:
        payload = artifacts[event["name"]]
        event.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))


def test_live_checker_accepts_host_artifacts_and_rejects_model_text(sample):
    measured, artifacts = live_evidence(sample)
    output = evidence_log(measured)
    assert sample.checker.assess(output, artifacts) == []
    scaffold = importlib.import_module("_scaffold")
    assert sample.checker.assess(scaffold.quoted(output), artifacts)
    assert sample.checker.assess(output, {**artifacts, "estimate.md": b"tampered"})


def test_pricing_diagnostics_do_not_qualify_an_unresolved_fixture_price(sample):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    disk = next(row for row in report["rows"] if row["product_key"] == "premium-ssd-p10")
    disk.update(price=None, monthly_cost=None, status="Pricing lookup failed")
    report.update(unresolved_resources=2, pricing_failures={"premium-ssd-p10": {"stage": "lookup"}})
    measured[-1]["unresolved"] = 2
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts) == [
        "Expected three priced rows and one unresolved row"
    ]


@pytest.mark.parametrize("product_key", ["linux-b2s", "linux-d2s-v5", "premium-ssd-p10"])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("serviceName", "Wrong service"),
        ("productName", "Virtual Machines BS Series Windows"),
        ("skuName", "Wrong SKU"),
        ("meterName", "Wrong meter"),
    ],
)
def test_live_checker_rejects_wrong_meter_with_consistent_artifacts(
    sample, product_key, field, replacement
):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    row = next(row for row in report["rows"] if row["product_key"] == product_key)
    row["price"][field] = replacement
    row["price"]["retailPrice"] = 0.25
    row["monthly_cost"] = str(
        Decimal("0.25") * row["quantity"] * Decimal(row["usage_per_resource"])
    )
    report["estimated_subtotal"] = str(
        sum(Decimal(item["monthly_cost"]) for item in report["rows"] if item["price"])
    )
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts) == [
        "Selected meter does not match the fixture product"
    ]


def test_live_checker_identity_is_independent_of_sample_catalog(sample, monkeypatch):
    monkeypatch.setitem(
        sample.estimate.CATALOG, "linux-b2s", sample.estimate.CATALOG["windows-b2s"]
    )
    measured, artifacts = live_evidence(sample)
    assert sample.checker.assess(evidence_log(measured), artifacts) == [
        "Selected meter does not match the fixture product"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("isPrimaryMeterRegion", False),
        ("isPrimaryMeterRegion", 1),
        ("isPrimaryMeterRegion", "true"),
        ("isPrimaryMeterRegion", None),
        ("effectiveStartDate", "invalid"),
        ("effectiveStartDate", "2026-01-01T00:00:00"),
        ("effectiveStartDate", "2099-01-01T00:00:00Z"),
        ("effectiveStartDate", "2026-01-01T01:00:00-01:00"),
        ("meterId", 123),
        ("reservationTerm", "1 Year"),
        ("retailPrice", "0.05"),
    ],
)
def test_live_checker_rejects_inadmissible_meter_with_consistent_artifacts(sample, field, value):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    report["queried_at_utc"] = "2026-01-01T00:00:00Z"
    report["rows"][0]["price"][field] = value
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts)


@pytest.mark.parametrize("retrieved", ["invalid", "2026-01-01", None])
def test_live_checker_rejects_invalid_retrieval_time(sample, retrieved):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    report["queried_at_utc"] = retrieved
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts)


def test_live_checker_compares_effective_instants_with_timezone_offsets(sample):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    report["queried_at_utc"] = "2026-01-01T00:00:00Z"
    report["rows"][0]["price"]["effectiveStartDate"] = "2026-01-01T01:00:00+01:00"
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts) == []


@pytest.mark.parametrize(
    "case",
    [
        "warning-only",
        "missing-row",
        "duplicate-row",
        "name",
        "product",
        "quantity",
        "unit-price",
        "unit",
        "usage",
        "cost",
        "notes",
        "status",
        "assumption",
        "subtotal",
        "currency",
        "region",
        "retrieved",
        "unresolved",
        "unknown-cost",
    ],
)
def test_live_checker_rejects_inconsistent_markdown_with_matching_hash(sample, case):
    measured, artifacts = live_evidence(sample)
    display = json.loads(artifacts["estimate.json"])
    row = display["rows"][0]
    changes = {
        "name": (row, "name", "Other resource"),
        "product": (row, "product_key", "windows-b2s"),
        "quantity": (row, "quantity", 3),
        "unit-price": (row["price"], "retailPrice", 0.01),
        "unit": (row["price"], "unitOfMeasure", "100 Hours"),
        "usage": (row, "usage_per_resource", "1"),
        "cost": (row, "monthly_cost", "0.01"),
        "notes": (row, "notes", "Different notes"),
        "status": (row, "status", "Not priced"),
        "assumption": (row, "assumption", "Different assumption"),
        "subtotal": (display, "estimated_subtotal", "0.01"),
        "currency": (display, "currency", "EUR"),
        "region": (display, "region", "eastus"),
        "retrieved": (display, "queried_at_utc", "2026-01-01T00:00:00Z"),
        "unresolved": (display, "unresolved_resources", 2),
        "unknown-cost": (display["rows"][-1], "monthly_cost", "0.01"),
    }
    if case in changes:
        target, key, value = changes[case]
        target[key] = value
    rendered = sample.estimate.markdown(display)
    if case == "warning-only":
        rendered = "No complete inventory total is available."
    elif case == "missing-row":
        rendered = "\n".join(line for index, line in enumerate(rendered.splitlines()) if index != 6)
    elif case == "duplicate-row":
        rendered += rendered.splitlines()[6] + "\n"
    artifacts["estimate.md"] = rendered.encode()
    measured[9].update(
        sha256=hashlib.sha256(artifacts["estimate.md"]).hexdigest(),
        bytes=len(artifacts["estimate.md"]),
    )
    assert sample.checker.assess(evidence_log(measured), artifacts)


def test_live_checker_accepts_escaped_markdown_content(sample):
    measured, artifacts = live_evidence(sample)
    report = json.loads(artifacts["estimate.json"])
    report["rows"][0]["name"] = "Web | <servers> \\ *[one]*\nnew"
    report["rows"][0]["notes"] = "Use `literal_text` & keep \\| punctuation"
    retain_live_report(sample, measured, artifacts, report)
    assert sample.checker.assess(evidence_log(measured), artifacts) == []


@pytest.mark.parametrize(
    "case", ["cleanup", "order", "timing", "call-id", "source", "total", "arithmetic", "quantity"]
)
def test_live_checker_rejects_incomplete_or_inconsistent_evidence(sample, case):
    measured, artifacts = live_evidence(sample)
    if case == "cleanup":
        measured[7]["complete"] = False
    elif case == "order":
        measured[1], measured[3] = measured[3], measured[1]
    elif case == "timing":
        measured[0]["seconds"] = -1
    elif case == "call-id":
        measured[2]["call"] = measured[0]["call"]
    else:
        report = json.loads(artifacts["estimate.json"])
        if case == "source":
            report["inventory_source"] = "fixture"
        elif case == "total":
            report["inventory_total"] = report["estimated_subtotal"]
        elif case == "quantity":
            report["rows"][0]["quantity"] = 3
        else:
            report["rows"][0]["monthly_cost"] = "0.01"
        artifacts["estimate.json"] = json.dumps(report).encode()
        measured[8].update(
            sha256=hashlib.sha256(artifacts["estimate.json"]).hexdigest(),
            bytes=len(artifacts["estimate.json"]),
        )
    assert sample.checker.assess(evidence_log(measured), artifacts)
