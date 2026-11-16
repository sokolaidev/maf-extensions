"""The paired prompt comparison must retain failures and avoid claiming a semantic verdict."""

import argparse
import asyncio
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agent_framework import ChatResponse, Message
from maf_cachebench._providers import ProviderRuntime


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
def test_retention_harness_matches_salts_and_omits_summary_temperature(
    comparison, monkeypatch, variant
):
    retention = importlib.import_module("compare_compaction_retention")
    current = comparison.prompts()
    previous = json.loads(comparison.BASELINE.read_text(encoding="utf-8"))["prompts"]
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
    assert retention.main() == 0
    assert options == [
        {"temperature": None, "response_max_tokens": 1024, "model": "synthetic-model"}
    ]
    assert configured_prompts() == current
