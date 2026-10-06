"""CLI selections and offline reports fail honestly before provider work."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from maf_cachebench import ModelPricing, _advise_cli, _cli, _recall_cli, _summary_cli
from maf_cachebench._strategies import STRATEGIES_FORCING_RECORDS, STRATEGIES_NEEDING_SUMMARIZER

pytestmark = pytest.mark.anyio

COMMANDS = [
    (_advise_cli, _advise_cli.run_advice),
    (_recall_cli, _recall_cli.run_recall),
    (_summary_cli, _summary_cli.run_summary),
]


@pytest.mark.parametrize("module,run", COMMANDS)
@pytest.mark.parametrize(
    "selection",
    [*sorted(STRATEGIES_NEEDING_SUMMARIZER | STRATEGIES_FORCING_RECORDS), "", "unknown"],
)
async def test_unsupported_strategies_fail_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch, module: Any, run: Any, selection: str
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid strategies must fail before provider/tokenizer setup")

    for name in ("build_provider", "build_tokenizer"):
        monkeypatch.setattr(module, name, unexpected)
    args = module.build_parser().parse_args(["azure", "--strategies", selection])
    with pytest.raises(SystemExit, match="strategy|strategies"):
        await run(args)


@pytest.mark.parametrize("module,run", COMMANDS)
def test_simple_cli_help_does_not_advertise_summarizers(module: Any, run: Any) -> None:
    action = next(a for a in module.build_parser()._actions if a.dest == "strategies")
    advertised = action.help.removeprefix("Available: ").split(",")
    assert not (set(advertised) & STRATEGIES_NEEDING_SUMMARIZER)
    assert "none" in advertised


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_recall_rejects_nonpositive_repeats(value: str) -> None:
    args = _recall_cli.build_parser().parse_args(["azure", "--repeats", value])
    with pytest.raises(SystemExit, match="--repeats must be greater than 0"):
        await _recall_cli.run_recall(args)


@pytest.mark.parametrize("option", ["--providers", "--strategies", "--sizes"])
async def test_replay_rejects_empty_selection(tmp_path: Path, option: str) -> None:
    out = tmp_path / "out"
    args = _cli.build_parser().parse_args([option, "", "--out", str(out)])
    with pytest.raises(SystemExit, match="At least one"):
        await _cli.run_benchmark(args)
    assert not out.exists()


async def test_replay_fails_when_every_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise ValueError("missing credentials")

    monkeypatch.setattr(_cli, "build_provider", unavailable)
    args = _cli.build_parser().parse_args(
        [
            "--providers",
            "azure,mistral",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
            "--run-id",
            "empty",
        ]
    )
    assert await _cli.run_benchmark(args) == 1
    assert "No benchmark cells ran" in capsys.readouterr().out
    assert not (tmp_path / "empty-summary.csv").exists()


async def test_dry_run_preserves_local_prompt_metrics(tmp_path: Path) -> None:
    args = _cli.build_parser().parse_args(
        [
            "--dry-run",
            "--providers",
            "azure",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
            "--run-id",
            "dry",
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    with (tmp_path / "dry-summary.csv").open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert int(row["total_local_sent_tokens"]) > 0
    assert float(row["local_reusable_ratio"]) > 0
    assert int(row["total_input_tokens"]) == 0


async def test_replay_continues_after_one_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    def provider(name: str, **kwargs: Any) -> Any:
        if name == "azure":
            raise ValueError("missing credentials")
        return SimpleNamespace(model="stub")

    monkeypatch.setattr(_cli, "build_provider", provider)
    monkeypatch.setattr(_cli, "ProviderCaller", lambda *args, **kwargs: _cli._DryRunCaller())
    args = _cli.build_parser().parse_args(
        [
            "--providers",
            "azure,mistral",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
            "--run-id",
            "partial",
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    assert "skipping azure" in capsys.readouterr().out
    with (tmp_path / "partial-summary.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["provider"] == "mistral"


@pytest.mark.parametrize("provider", ["azure", "azure-responses", "foundry", "mistral", "ollama"])
async def test_advice_requires_pricing_before_measurement(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    async def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Missing prices must fail before measurement")

    monkeypatch.setattr(_advise_cli, "_measure", unexpected)
    with pytest.raises(SystemExit, match="--price-input is required"):
        await _advise_cli.run_advice(_advise_cli.build_parser().parse_args([provider]))


async def test_advice_resolves_default_catalogue_model_before_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    runtime = SimpleNamespace(model="resolved-model")
    events: list[str] = []
    monkeypatch.setattr(_advise_cli, "build_provider", lambda *a, **k: runtime)

    def catalogue(model: str) -> Any:
        assert model == "resolved-model"
        events.append("pricing")
        return ModelPricing(1, 0.1)

    async def measure(args: Any, provider: str, configured: Any) -> Any:
        assert configured is runtime
        assert events == ["pricing"]
        events.append("measurement")
        return []

    monkeypatch.setattr(_advise_cli, "fetch_openrouter_pricing", catalogue)
    monkeypatch.setattr(_advise_cli, "_measure", measure)
    with pytest.raises(SystemExit, match="Cannot advise"):
        await _advise_cli.run_advice(_advise_cli.build_parser().parse_args(["openrouter"]))
    assert events == ["pricing", "measurement"]


@pytest.mark.parametrize("module_name", ["_advise_cli", "_summary_cli", "_live_cli"])
@pytest.mark.parametrize("failure", ["transport", "status"])
def test_catalogue_failures_have_actionable_cli_errors(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    failure: str,
) -> None:
    import importlib

    import httpx

    def get(*args: Any, **kwargs: Any) -> Any:
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
        if failure == "transport":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx, "get", get)
    module = importlib.import_module(f"maf_cachebench.{module_name}")
    args = module.build_parser().parse_args(["openrouter:model"])
    with pytest.raises(SystemExit, match="(?s)Could not fetch pricing.*Pass --price-input"):
        module._resolve_pricing(args, "openrouter", "model")


@pytest.mark.parametrize("name", ["summarization", "token_budget_summarize"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_replay_rejects_unconfigured_summarizers_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    dry_run: bool,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unconfigured summarizers must fail before setup")

    monkeypatch.setattr(_cli, "build_tokenizer", unexpected)
    argv = ["--strategies", f"none,{name}"]
    if dry_run:
        argv += ["--dry-run", "--summarizer-provider", "azure"]
    with pytest.raises(SystemExit, match="--summarizer-provider|--dry-run"):
        await _cli.run_benchmark(_cli.build_parser().parse_args(argv))


@pytest.mark.parametrize(
    "name", ["tool_summary_anchored", "user_summary_anchored", "tool_and_user_summary_anchored"]
)
async def test_replay_rejects_live_middleware_strategies(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Live-only strategies must fail before replay setup")

    monkeypatch.setattr(_cli, "build_tokenizer", unexpected)
    with pytest.raises(SystemExit, match="strategy"):
        await _cli.run_benchmark(_cli.build_parser().parse_args(["--strategies", f"none,{name}"]))


async def test_replay_dry_run_ignores_unused_summarizer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Dry runs must not construct provider clients")

    monkeypatch.setattr(_cli, "build_provider", unexpected)
    args = _cli.build_parser().parse_args(
        [
            "--dry-run",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--summarizer-provider",
            "azure:model",
            "--out",
            str(tmp_path),
        ]
    )
    assert await _cli.run_benchmark(args) == 0


async def test_replay_resolves_summarizer_model_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from maf_cachebench._tokenizers import build_tokenizer

    seen: list[tuple[str, str | None]] = []

    def build(provider: str, **kwargs: Any) -> Any:
        seen.append((provider, kwargs.get("model")))
        return SimpleNamespace(client=object())

    monkeypatch.setattr(_cli, "build_provider", build)
    args = _cli.build_parser().parse_args(["--summarizer-provider", "azure:summarizer-model"])
    args.run_id = "selector"
    assert await _cli._run_matrix(
        args,
        tokenizer=build_tokenizer("estimator"),
        providers=[],
        sizes=[],
        strategies=[],
        on_record=None,
    ) == ([], [])
    assert seen == [("azure", "summarizer-model")]


@pytest.mark.parametrize("phase", ["first", "interim", "final"])
@pytest.mark.parametrize("failure", ["error", "missing", "zero", "negative", "none"])
async def test_summary_requires_every_call_to_have_valid_usage(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
) -> None:
    from types import SimpleNamespace

    from maf_cachebench import CallOutcome, build_recall_scenario

    scenario = build_recall_scenario(salt="valid-usage", filler_turns=0, filler_tokens=0)
    total = len(scenario.transcript.turns)
    target = {"first": 1, "interim": 2, "final": total}[phase]
    calls = 0

    async def caller(messages: Any) -> CallOutcome:
        nonlocal calls
        calls += 1
        if calls == target and failure != "none":
            return CallOutcome(
                latency_ms=0,
                error="failed request" if failure == "error" else None,
                input_tokens={"error": 100, "missing": None, "zero": 0, "negative": -1}[failure],
            )
        return CallOutcome(latency_ms=0, input_tokens=100, cached_tokens=50, text="answer")

    monkeypatch.setattr(_summary_cli, "build_provider", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(_summary_cli, "ProviderCaller", lambda *a, **k: caller)
    monkeypatch.setattr(_summary_cli, "build_recall_scenario", lambda **k: scenario)
    args = _summary_cli.build_parser().parse_args(["azure", "--tokenizer", "estimator"])
    if failure == "none":
        measured = await _summary_cli._measure(args, "azure", None, "none", ModelPricing(1, 0.1))
        assert measured.input_tokens == total * 100
        assert measured.score.error is None
        assert calls == total
    else:
        with pytest.raises(SystemExit, match="Cannot measure"):
            await _summary_cli._measure(args, "azure", None, "none", ModelPricing(1, 0.1))
        assert calls == target


@pytest.mark.parametrize(
    "option", ["--price-long-cached", "--price-long-output", "--price-long-cache-write"]
)
@pytest.mark.parametrize("value", ["0", "1"])
@pytest.mark.parametrize("provider", ["azure", "openrouter"])
def test_orphan_long_tier_prices_fail_before_catalogue_lookup(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    value: str,
    provider: str,
) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid prices must fail before catalogue lookup")

    monkeypatch.setattr(_live_cli, "fetch_openrouter_pricing", unexpected)
    argv = [provider, option, value]
    if provider == "azure":
        argv += ["--price-input", "1"]
    args = _live_cli.build_parser().parse_args(argv)
    with pytest.raises(SystemExit, match="--price-long-input"):
        _live_cli._resolve_pricing(args, provider, "stub")
