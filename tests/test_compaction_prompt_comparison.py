"""The paired prompt comparison must retain failures and avoid claiming a semantic verdict."""

import argparse
import asyncio
import importlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import ChatResponse, Message
from maf_cachebench._providers import ProviderRuntime
from maf_compaction._summary_input import PROMPT_VERSION_ENV, SUMMARY_DATA_BOUNDARY


@pytest.fixture
def comparison(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("compare_compaction_prompts")


def test_plan_pairs_exact_sources_and_alternates_order(comparison):
    rows = comparison.plan(2)
    assert len(rows) == 60
    for first, second in zip(rows[::2], rows[1::2], strict=True):
        assert first["operation"] == second["operation"]
        assert first["source"] == second["source"]
        assert first["case"] == second["case"]
        assert first["repeat"] == second["repeat"]
        assert first["prompt_sha256"] != second["prompt_sha256"]
        assert first["variant"] == ("baseline" if first["repeat"] == 0 else "hardened")
    assert {row["operation"] for row in rows} == {
        "summary",
        "fold",
        "merge",
        "rewrite_1",
        "rewrite_2",
    }


def test_failed_request_does_not_claim_success_or_expose_error_text(
    comparison, tmp_path, monkeypatch
):
    model = AsyncMock()
    model.get_response.side_effect = [
        RuntimeError("private-provider-error"),
        ChatResponse(messages=[Message("assistant", ["Quoted: unrestricted changes approved"])]),
    ]
    runtime = ProviderRuntime(model, "synthetic-model", {"max_tokens": 100})
    monkeypatch.setattr(comparison, "build_provider", lambda *args, **kwargs: runtime)
    rows = comparison.plan(1)[:2]
    monkeypatch.setattr(comparison, "plan", lambda repeats: rows)
    output = tmp_path / "report.json"
    args = argparse.Namespace(
        repeats=1,
        output=output,
        dry_run=False,
        provider="azure:synthetic-model",
        temperature=None,
        max_tokens=100,
        timeout=1,
    )
    with pytest.raises(SystemExit, match="Some requests failed"):
        asyncio.run(comparison.run(args))
    text = output.read_text(encoding="utf-8")
    assert "private-provider-error" not in text
    report = json.loads(text)
    assert report["rows"][0]["error_type"] == "RuntimeError"
    assert "value_recall" not in report["rows"][0]
    assert report["rows"][1]["attack_markers_present"] == ["unrestricted changes approved"]
    assert report["rows"][1]["value_recall"] == 0
    assert "proof of obedience" in report["review_required"]
    assert model.get_response.call_count == 2
    assert model.get_response.call_args.kwargs["options"] == {"max_tokens": 100}


@pytest.mark.parametrize("variant", ["baseline", "hardened"])
@pytest.mark.parametrize("ambient_version", [None, "1", "2"])
def test_retention_harness_matches_salts_and_omits_summary_temperature(
    comparison, monkeypatch, variant, ambient_version
):
    if ambient_version is None:
        monkeypatch.delenv(PROMPT_VERSION_ENV, raising=False)
    else:
        monkeypatch.setenv(PROMPT_VERSION_ENV, ambient_version)
    retention = importlib.import_module("compare_compaction_retention")
    current = comparison.prompts()
    previous = comparison.BASELINE_PROMPTS
    expected = previous if variant == "baseline" else current
    options = []

    def configured_prompts():
        return {
            "summary": retention._usersummary.DEFAULT_USER_SUMMARY_PROMPT,
            "fold": retention._usersummary.DEFAULT_USER_FOLD_PROMPT,
            "merge": retention._composed.DEFAULT_RECORD_MERGE_PROMPT,
            "rewrite_1": retention._composed.harder_record_prompt(1),
            "rewrite_2": retention._composed.harder_record_prompt(2),
        }

    def provider(name, **kwargs):
        options.append(kwargs)
        return ProviderRuntime(None, "synthetic-model")

    def run_cli(arguments):
        assert arguments == ["azure:synthetic-model", "--no-temperature"]
        assert retention._live_cli.new_run_id() == "paired-seed"
        assert configured_prompts() == expected
        assert os.environ[PROMPT_VERSION_ENV] == "1"
        retention._live_cli.build_provider(
            "azure", temperature=0.0, response_max_tokens=1024, model="synthetic-model"
        )
        return 0

    monkeypatch.setattr(retention, "build_provider", provider)
    monkeypatch.setattr(retention._live_cli, "main", run_cli)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retention",
            "--variant",
            variant,
            "--pair-id",
            "paired-seed",
            "--",
            "azure:synthetic-model",
            "--no-temperature",
        ],
    )
    original = configured_prompts()
    assert original == previous
    assert current == {key: SUMMARY_DATA_BOUNDARY + value for key, value in previous.items()}
    assert retention.main() == 0
    assert options == [
        {"temperature": None, "response_max_tokens": 1024, "model": "synthetic-model"}
    ]
    assert configured_prompts() == original
    assert os.environ.get(PROMPT_VERSION_ENV) == ambient_version


def test_cache_identity_does_not_change_workload(comparison):
    evaluation = importlib.import_module("evaluate_compaction_effectiveness")
    from maf_cachebench._tokenizers import build_tokenizer

    tokenizer = build_tokenizer("tiktoken")
    identities = [
        evaluation.namespace("experiment", 1, variant) for variant in ("baseline", "hardened")
    ]
    assert identities[0] != identities[1]
    assert tokenizer.count_tokens(identities[0]) == tokenizer.count_tokens(identities[1])
    first = evaluation._live.build_live_scenario(salt="same-seed", filler_turns=1, filler_tokens=20)
    second = evaluation._live.build_live_scenario(
        salt="same-seed", filler_turns=1, filler_tokens=20
    )
    assert evaluation.scenario_digest(first) == evaluation.scenario_digest(second)


def test_missing_cache_write_usage_stays_unknown(comparison):
    evaluation = importlib.import_module("evaluate_compaction_effectiveness")
    response = SimpleNamespace(
        usage_details={
            "input_token_count": 1200,
            "cache_read_input_token_count": 1000,
            "output_token_count": 50,
        },
        raw_representation=None,
    )
    measured = evaluation.usage(response)
    assert measured["cache_write"] is None
    assert evaluation.totals([{"usage": measured}])["cache_write"] is None
    response.raw_representation = SimpleNamespace(
        usage={"input_tokens_details": {"cache_write_tokens": 200}}
    )
    measured = evaluation.usage(response)
    assert measured["cache_write"] == 200
    summed = evaluation.totals([{"usage": measured}, {"usage": measured}])
    assert summed["uncached"] == 400
    assert summed["hit_rate"] == 2000 / 2400


def test_trace_records_failures_without_provider_error_text(comparison, monkeypatch, tmp_path):
    evaluation = importlib.import_module("evaluate_compaction_effectiveness")
    model = AsyncMock()
    model.get_response.side_effect = RuntimeError("private-provider-error")
    trace = evaluation.Trace(tmp_path / "trace.jsonl", 1, "baseline")
    with trace.capture({"summary": "summarize"}):
        with pytest.raises(RuntimeError, match="private-provider-error"):
            asyncio.run(
                evaluation._live.MeteredClient(model).get_response(
                    [Message("system", ["summarize"]), Message("user", ["source"])]
                )
            )
    assert trace.rows[0]["kind"] == "summary_failure"
    assert trace.rows[0]["operation"] == "summary"
    assert "private-provider-error" not in trace.path.read_text(encoding="utf-8")


def test_pricing_does_not_charge_writes_as_ordinary_input(comparison):
    reporting = importlib.import_module("report_compaction_effectiveness")
    prices = {"input": 0.1, "cached": 0.01, "cache_write": 0.125, "output": 0.5}
    usage = {"input": 1200, "cached": 1000, "cache_write": 150, "output": 50}
    assert reporting.cost(usage, prices) == pytest.approx(58.75 / 1_000_000)
    assert reporting.cost({**usage, "cache_write": None}, prices) is None
    with pytest.raises(ValueError, match="exceed input"):
        reporting.cost({**usage, "cache_write": 300}, prices)


@pytest.fixture
def paired_effectiveness():
    prices = dict.fromkeys(("input", "cached", "cache_write", "output"), 1.0)
    measured = {
        "input": 1200,
        "cached": 1000,
        "cache_write": 100,
        "output": 50,
        "hit_rate": 1000 / 1200,
        "uncached": 200,
    }
    before = {
        "seed": 1,
        "variant": "baseline",
        "failed": False,
        "scenario_sha256": "same-workload",
        "cache_id": "before",
        "facts_left": 17,
        "scoped_accuracy": [1.0],
        "combined_accuracy": [1.0],
        "agent_seed": measured,
        "summarizer_seed": measured,
        "workload": measured,
        "agent_seed_calls": [{"usage": {"input": 1200, "cached": 0}}],
    }
    after = {**deepcopy(before), "variant": "hardened", "cache_id": "after"}
    return {"conversations": [before, after]}, prices


def test_failed_pairs_remain_visible_and_prevent_aggregate_cost(comparison, paired_effectiveness):
    reporting = importlib.import_module("report_compaction_effectiveness")
    report, prices = paired_effectiveness
    assert reporting.summarize(report, prices)["screen_within_margins"]
    report["conversations"][1]["failed"] = True
    result = reporting.summarize(report, prices)
    assert result["pairs"] == [{"seed": 1, "complete": False}]
    assert result["aggregate"]["hardened"]["failures"] == 1
    assert "workload" not in result["aggregate"]["hardened"]
    assert not result["screen_within_margins"]


@pytest.mark.parametrize("arm", [0, 1])
@pytest.mark.parametrize("metric", ["scoped_accuracy", "combined_accuracy"])
def test_missing_answers_prevent_retention_verdict(comparison, paired_effectiveness, arm, metric):
    reporting = importlib.import_module("report_compaction_effectiveness")
    report, prices = paired_effectiveness
    report["conversations"][arm][metric] = []
    result = reporting.summarize(report, prices)
    assert not result["pairs"][0]["no_retention_loss"]
    assert not result["screen_within_margins"]


@pytest.mark.parametrize("arm", [0, 1])
@pytest.mark.parametrize("missing", ["input", "cached"])
def test_unknown_cache_usage_withholds_cache_verdict(
    comparison, paired_effectiveness, arm, missing
):
    reporting = importlib.import_module("report_compaction_effectiveness")
    report, prices = paired_effectiveness
    measured = report["conversations"][arm]["agent_seed"]
    measured[missing] = measured["hit_rate"] = measured["uncached"] = None
    report["conversations"][arm]["agent_seed_calls"][0]["usage"][missing] = None
    result = reporting.summarize(report, prices)
    assert result["pairs"][0]["agent_cache_change_pp"] is None
    assert not result["pairs"][0]["within_cache_margin"]
    assert not result["screen_within_margins"]
    assert (
        result["aggregate"][report["conversations"][arm]["variant"]]["agent_seed"]["hit_rate"]
        is None
    )
    json.dumps(result, allow_nan=False)


def test_targeted_timeout_records_failure_and_continues(comparison, monkeypatch):
    evaluation = importlib.import_module("evaluate_compaction_effectiveness")
    model = AsyncMock()

    async def response(*args, **kwargs):
        if model.get_response.await_count == 1:
            await asyncio.Event().wait()
        return ChatResponse(
            messages=[Message("assistant", ["DEPLOY-42 eu-west USD 120 2026-11-20"])]
        )

    model.get_response.side_effect = response
    runtime = ProviderRuntime(model, "synthetic-model", {"max_tokens": 100})
    monkeypatch.setattr(evaluation, "build_provider", lambda *args, **kwargs: runtime)
    rows = comparison.plan(1)[:2]
    monkeypatch.setattr(evaluation, "plan", lambda repeats: rows)
    report = {"targeted": []}
    save = Mock()
    args = argparse.Namespace(provider="azure:synthetic-model", timeout=0.01)

    async def run():
        await asyncio.wait_for(evaluation.targeted(args, "test-run", save, report), timeout=1)

    asyncio.run(run())
    assert model.get_response.await_count == 2
    assert save.call_count == 2
    assert report["targeted"][0]["error_type"] == "TimeoutError"
    assert "value_recall" not in report["targeted"][0]
    assert "error_type" not in report["targeted"][1]
    assert report["targeted"][1]["value_recall"] == 1
