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
        return SimpleNamespace(client=object(), options={})

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


@pytest.mark.parametrize(
    "option,selection",
    [
        ("--providers", "azure:model,azure:model"),
        ("--strategies", "none,none"),
        ("--sizes", "small,small"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
async def test_replay_duplicates_fail_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    option: str,
    selection: str,
    dry_run: bool,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Duplicate selections must fail before setup")

    monkeypatch.setattr(_cli, "build_tokenizer", unexpected)
    output = tmp_path / "records"
    argv = [option, selection, "--out", str(output)] + (["--dry-run"] if dry_run else [])
    with pytest.raises(SystemExit, match="Duplicate"):
        await _cli.run_benchmark(_cli.build_parser().parse_args(argv))
    assert not output.exists()


async def test_replay_allows_distinct_models_on_one_provider(tmp_path: Path) -> None:
    args = _cli.build_parser().parse_args(
        [
            "--dry-run",
            "--providers",
            "azure:first,azure:second",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
            "--run-id",
            "models",
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    with (tmp_path / "models-summary.csv").open(encoding="utf-8", newline="") as stream:
        assert {row["model"] for row in csv.DictReader(stream)} == {"first", "second"}


@pytest.mark.parametrize("module,run", COMMANDS)
async def test_standalone_duplicate_strategies_fail_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    run: Any,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Duplicate strategies must fail before setup")

    monkeypatch.setattr(module, "build_tokenizer", unexpected)
    with pytest.raises(SystemExit, match="Duplicate"):
        await run(module.build_parser().parse_args(["azure", "--strategies", "none,none"]))


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_unknown_summarizer_fails_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live: bool,
    dry_run: bool,
) -> None:
    from maf_cachebench import _live_cli

    module = _live_cli if live else _cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unknown summarizers must fail before setup")

    for name in ("build_provider", "build_tokenizer"):
        monkeypatch.setattr(module, name, unexpected)
    output = tmp_path / "records"
    argv = (
        (["azure"] if live else ["--out", str(output)])
        + [
            "--strategies",
            "none,summarization",
            "--summarizer-provider",
            "unknown:model",
        ]
        + (["--dry-run"] if dry_run else [])
    )
    args = module.build_parser().parse_args(argv)
    with pytest.raises(SystemExit, match="Unknown summarizer provider"):
        if live:
            await _live_cli.run_live_comparison(args)
        else:
            await _cli.run_benchmark(args)
    assert not output.exists()


async def test_live_duplicate_strategies_fail_before_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Duplicate strategies must fail before setup")

    monkeypatch.setattr(_live_cli, "build_tokenizer", unexpected)
    with pytest.raises(SystemExit, match="Duplicate"):
        await _live_cli.run_live_comparison(
            _live_cli.build_parser().parse_args(["azure", "--strategies", "none,none"])
        )


@pytest.mark.parametrize("cached,normalized", [(None, 0), (-50, 0), (400, 100), (50, 50)])
async def test_summary_bounds_cache_per_call(
    monkeypatch: pytest.MonkeyPatch,
    cached: int | None,
    normalized: int,
) -> None:
    from types import SimpleNamespace

    from maf_cachebench import CallOutcome

    calls = 0

    async def caller(messages: Any) -> CallOutcome:
        nonlocal calls
        calls += 1
        return CallOutcome(
            latency_ms=0, input_tokens=100, cached_tokens=cached if calls % 2 else 0, text="answer"
        )

    monkeypatch.setattr(_summary_cli, "build_provider", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(_summary_cli, "ProviderCaller", lambda *a, **k: caller)
    args = _summary_cli.build_parser().parse_args(
        ["azure", "--tokenizer", "estimator", "--filler-turns", "0"]
    )
    outcome = await _summary_cli._measure(args, "azure", None, "none", ModelPricing(1, 0.1))
    expected = normalized * ((calls + 1) // 2)
    assert calls > 1
    assert outcome.cached_tokens == expected
    assert outcome.cache_reported is (cached is not None)
    assert outcome.cost == pytest.approx(((calls * 100 - expected) + expected * 0.1) / 1_000_000)


@pytest.mark.parametrize("module_name", ["_advise_cli", "_summary_cli", "_live_cli"])
@pytest.mark.parametrize("value", ["0", "1"])
async def test_orphan_cached_price_fails_before_setup(
    monkeypatch: pytest.MonkeyPatch, module_name: str, value: str
) -> None:
    import importlib

    module = importlib.import_module(f"maf_cachebench.{module_name}")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Orphan prices must fail before provider or catalogue setup")

    for name in ("build_provider", "build_tokenizer", "fetch_openrouter_pricing"):
        monkeypatch.setattr(module, name, unexpected)
    args = module.build_parser().parse_args(["openrouter:model", "--price-cached", value])
    run = getattr(
        module,
        {
            "_advise_cli": "run_advice",
            "_summary_cli": "run_summary",
            "_live_cli": "run_live_comparison",
        }[module_name],
    )
    with pytest.raises(SystemExit, match="--price-cached needs --price-input"):
        await run(args)


@pytest.mark.parametrize("module,run", [COMMANDS[0], COMMANDS[2]])
async def test_recommendation_requires_baseline_before_setup(
    monkeypatch: pytest.MonkeyPatch, module: Any, run: Any
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Missing baseline must fail before setup")

    monkeypatch.setattr(module, "build_provider", unexpected)
    with pytest.raises(SystemExit, match="'none' baseline"):
        await run(module.build_parser().parse_args(["azure", "--strategies", "truncation"]))


@pytest.mark.parametrize(
    "option,value",
    [
        ("--tool-turns", "0"),
        ("--tool-turns", "2"),
        ("--markers-per-tool", "0"),
        ("--filler-tool-turns", "-1"),
        ("--filler-turns", "-1"),
        ("--filler-tokens", "-1"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
async def test_live_rejects_clamped_workload_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str, dry_run: bool
) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid counts must fail before tokenizer or provider setup")

    monkeypatch.setattr(_live_cli, "build_tokenizer", unexpected)
    argv = ["azure", option, value] + (["--dry-run"] if dry_run else [])
    with pytest.raises(SystemExit, match=option):
        await _live_cli.run_live_comparison(_live_cli.build_parser().parse_args(argv))


@pytest.mark.parametrize("module,run", [COMMANDS[1], COMMANDS[2]])
@pytest.mark.parametrize("option", ["--filler-turns", "--filler-tokens"])
async def test_standalone_rejects_negative_workload_before_setup(
    monkeypatch: pytest.MonkeyPatch, module: Any, run: Any, option: str
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid counts must fail before provider setup")

    monkeypatch.setattr(module, "build_provider", unexpected)
    with pytest.raises(SystemExit, match=option):
        await run(module.build_parser().parse_args(["azure", option, "-1"]))


@pytest.mark.parametrize("selection", ["azure,azure:default", "azure:default,azure"])
async def test_replay_rejects_resolved_aliases_before_first_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: str
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(_cli, "build_provider", lambda *a, **k: SimpleNamespace(model="default"))

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Resolved aliases must fail before any cells or paid calls")

    monkeypatch.setattr(_cli, "build_preset", unexpected)
    args = _cli.build_parser().parse_args(
        [
            "--providers",
            selection,
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
        ]
    )
    with pytest.raises(SystemExit, match="Duplicate resolved provider/model"):
        await _cli.run_benchmark(args)


async def test_replay_distinct_resolved_models_keep_distinct_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        _cli, "build_provider", lambda *a, **k: SimpleNamespace(model=k["model"] or "default")
    )
    monkeypatch.setattr(_cli, "ProviderCaller", lambda *a, **k: _cli._DryRunCaller())
    args = _cli.build_parser().parse_args(
        [
            "--providers",
            "azure,azure:other",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--out",
            str(tmp_path),
            "--run-id",
            "resolved",
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    with (tmp_path / "resolved-summary.csv").open(encoding="utf-8", newline="") as stream:
        assert {row["model"] for row in csv.DictReader(stream)} == {"default", "other"}


@pytest.mark.parametrize(
    "option",
    [
        "price-input",
        "price-cached",
        "price-output",
        "price-cache-write",
        "price-long-input",
        "price-long-cached",
        "price-long-output",
        "price-long-cache-write",
    ],
)
@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
async def test_live_rejects_invalid_rates_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid rates must fail before tokenizer or provider setup")

    monkeypatch.setattr(_live_cli, "build_tokenizer", unexpected)
    args = _live_cli.build_parser().parse_args(["azure", f"--{option}={value}"])
    with pytest.raises(SystemExit, match=f"--{option} must be finite and non-negative"):
        await _live_cli.run_live_comparison(args)


@pytest.mark.parametrize(
    "module_name", ["_cli", "_advise_cli", "_recall_cli", "_summary_cli", "_live_cli"]
)
def test_default_tokenizer_needs_no_optional_dependency(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    import importlib
    import sys

    from maf_cachebench._tokenizers import build_tokenizer

    monkeypatch.setitem(sys.modules, "tiktoken", None)
    module = importlib.import_module(f"maf_cachebench.{module_name}")
    args = module.build_parser().parse_args([] if module_name == "_cli" else ["azure"])
    tokenizer = build_tokenizer(args.tokenizer)
    assert tokenizer.count_tokens("A short prompt") > 0
    with pytest.raises(RuntimeError, match="requires the tiktoken package"):
        build_tokenizer("tiktoken")


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
async def test_replay_rejects_invalid_cache_ratio_before_output(tmp_path: Path, value: str) -> None:
    output = tmp_path / "records"
    args = _cli.build_parser().parse_args(
        ["--dry-run", f"--cache-read-ratio={value}", "--out", str(output)]
    )
    with pytest.raises(SystemExit, match="--cache-read-ratio must be finite and non-negative"):
        await _cli.run_benchmark(args)
    assert not output.exists()


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_live_rejects_nonpositive_long_threshold_before_setup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid threshold must fail before setup")

    monkeypatch.setattr(_live_cli, "build_tokenizer", unexpected)
    args = _live_cli.build_parser().parse_args(
        [
            "azure",
            "--price-input",
            "1",
            "--price-long-input",
            "2",
            "--long-context-threshold",
            value,
        ]
    )
    with pytest.raises(SystemExit, match="--long-context-threshold must be greater than 0"):
        await _live_cli.run_live_comparison(args)


@pytest.mark.parametrize(
    "module_name,from_records", [("_summary_cli", False), ("_live_cli", False), ("_live_cli", True)]
)
@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
async def test_invalid_correctness_fails_before_setup_or_record_loading(
    monkeypatch: pytest.MonkeyPatch, module_name: str, value: str, from_records: bool
) -> None:
    import importlib

    module = importlib.import_module(f"maf_cachebench.{module_name}")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid correctness must fail before setup or record loading")

    for name in ("build_provider", "build_tokenizer"):
        monkeypatch.setattr(module, name, unexpected)
    if module_name == "_live_cli":
        monkeypatch.setattr(module, "_render_from_records", unexpected)
    argv = ["--from-jsonl", "unused.jsonl"] if from_records else ["azure"]
    args = module.build_parser().parse_args([*argv, f"--min-correctness={value}"])
    run = module.run_summary if module_name == "_summary_cli" else module.run_live_comparison
    with pytest.raises(SystemExit, match="--min-correctness"):
        await run(args)


async def test_default_run_ids_isolate_same_second_replay_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    monkeypatch.setattr(time, "strftime", lambda *a: "fixed-second")
    identities = []
    for _ in range(2):
        args = _cli.build_parser().parse_args(
            ["--dry-run", "--strategies", "none", "--sizes", "small", "--out", str(tmp_path)]
        )
        assert await _cli.run_benchmark(args) == 0
        identities.append(args.run_id)
    assert len(set(identities)) == 2
    assert len(list(tmp_path.glob("*-records.jsonl"))) == 2


@pytest.mark.parametrize("module_name", ["_recall_cli", "_summary_cli", "_advise_cli"])
async def test_measurement_salts_isolate_same_second_calls(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    import importlib
    import time
    from types import SimpleNamespace

    module = importlib.import_module(f"maf_cachebench.{module_name}")
    monkeypatch.setattr(time, "strftime", lambda *a: "fixed-second")
    salts: list[str] = []

    class Captured(Exception):
        pass

    def capture(*args: Any, **kwargs: Any) -> Any:
        salts.append(kwargs["salt"])
        raise Captured

    monkeypatch.setattr(
        module, "build_preset" if module_name == "_advise_cli" else "build_recall_scenario", capture
    )
    args = module.build_parser().parse_args(["azure"])
    for _ in range(2):
        with pytest.raises(Captured):
            if module_name == "_recall_cli":
                await module._probe(args, "azure", None, "none", 1)
            elif module_name == "_summary_cli":
                await module._measure(args, "azure", None, "none", ModelPricing(1, 0.1))
            else:
                await module._measure(args, "azure", SimpleNamespace(model="stub"))
    assert len(set(salts)) == 2


async def test_replay_summarizer_preserves_provider_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from copy import deepcopy

    from maf_cachebench import ProviderRuntime

    defaults = {
        "temperature": 0.0,
        "max_tokens": 512,
        "extra_body": {"provider": {"only": ["pinned"]}, "usage": {"include": True}},
    }
    original = deepcopy(defaults)
    observed: list[dict[str, Any]] = []

    class Client:
        async def get_response(self, *args: Any, **kwargs: Any) -> None:
            observed.append(deepcopy(kwargs["options"]))
            kwargs["options"].clear()

    monkeypatch.setattr(
        _cli, "build_provider", lambda *a, **k: ProviderRuntime(Client(), "stub", defaults)
    )

    def strategy(name: str, options: Any) -> Any:
        return options.summarizer

    async def cell(**kwargs: Any) -> list[Any]:
        client = kwargs["strategy"]
        await client.get_response([])
        await client.get_response([], options={"max_tokens": 256, "extra_body": {"trace": True}})
        return []

    monkeypatch.setattr(_cli, "build_strategy", strategy)
    monkeypatch.setattr(_cli, "run_cell", cell)
    args = _cli.build_parser().parse_args(
        [
            "--providers",
            "azure",
            "--summarizer-provider",
            "openrouter:model",
            "--strategies",
            "summarization",
            "--sizes",
            "small",
            "--repeats",
            "1",
            "--tokenizer",
            "estimator",
            "--out",
            str(tmp_path),
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    assert observed[0] == original
    assert observed[1] == {
        **original,
        "max_tokens": 256,
        "extra_body": {**original["extra_body"], "trace": True},
    }
    assert defaults == original


@pytest.mark.parametrize(
    "run_id",
    [
        "../result",
        "..\\result",
        "/absolute",
        "C:\\result",
        "C:result",
        "\\\\server\\share",
        "nested/result",
        "nested\\result",
        "bad:stream",
        "",
        ".",
        "..",
        "bad\x00id",
    ],
)
async def test_replay_rejects_unsafe_run_id_before_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: str
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid run IDs must fail before setup")

    monkeypatch.setattr(_cli, "build_tokenizer", unexpected)
    monkeypatch.setattr(_cli, "build_provider", unexpected)
    out = tmp_path / "out"
    args = _cli.build_parser().parse_args(
        ["--run-id", run_id, "--out", str(out), "--strategies", "none"]
    )
    with pytest.raises(SystemExit, match="--run-id"):
        await _cli.run_benchmark(args)
    assert not out.exists()


async def test_replay_safe_run_id_keeps_both_outputs_under_out(tmp_path: Path) -> None:
    out = tmp_path / "out"
    args = _cli.build_parser().parse_args(
        [
            "--run-id",
            "trial-1.2_ok",
            "--out",
            str(out),
            "--dry-run",
            "--providers",
            "azure",
            "--strategies",
            "none",
            "--sizes",
            "small",
            "--repeats",
            "1",
        ]
    )
    assert await _cli.run_benchmark(args) == 0
    assert {p.name for p in out.iterdir()} == {
        "trial-1.2_ok-records.jsonl",
        "trial-1.2_ok-summary.csv",
    }


@pytest.mark.parametrize("entry", ["replay", "advisor", "summary", "recall"])
@pytest.mark.parametrize(
    "argv",
    [
        ["--context-window", "0"],
        ["--context-window", "-1"],
        ["--context-window", "100", "--max-output-tokens", "100"],
        ["--max-output-tokens", "-1"],
    ],
)
async def test_budget_preflight_preserves_output_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str, argv: list[str]
) -> None:
    module, run = {
        "replay": (_cli, _cli.run_benchmark),
        "advisor": (_advise_cli, _advise_cli.run_advice),
        "summary": (_summary_cli, _summary_cli.run_summary),
        "recall": (_recall_cli, _recall_cli.run_recall),
    }[entry]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Budget validation must precede provider and pricing setup")

    monkeypatch.setattr(module, "build_provider", unexpected)
    if hasattr(module, "_resolve_pricing"):
        monkeypatch.setattr(module, "_resolve_pricing", unexpected)
    out = tmp_path / "out"
    out.mkdir()
    archive = out / "existing-records.jsonl"
    archive.write_bytes(b"prior archive\n")
    if entry == "replay":
        selection = [
            "--providers",
            "azure",
            "--out",
            str(out),
            "--run-id",
            "existing",
            "--sizes",
            "small",
        ]
    else:
        selection = ["azure"]
        if entry == "advisor":
            selection += ["--out", str(out), "--size", "small"]
    args = module.build_parser().parse_args([*selection, "--strategies", "none,truncation", *argv])
    with pytest.raises(SystemExit, match="rejects this configuration"):
        await run(args)
    assert archive.read_bytes() == b"prior archive\n"
    assert list(out.iterdir()) == [archive]


@pytest.mark.parametrize("module,run", COMMANDS[:1] + COMMANDS[2:])
async def test_baseline_only_selection_fails_before_setup(
    monkeypatch: pytest.MonkeyPatch, module: Any, run: Any
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Comparison validation must precede setup")

    monkeypatch.setattr(module, "build_provider", unexpected)
    monkeypatch.setattr(module, "build_tokenizer", unexpected)
    with pytest.raises(SystemExit, match="non-none"):
        await run(module.build_parser().parse_args(["azure", "--strategies", "none"]))


@pytest.mark.parametrize("entry", ["replay", "advisor", "recall", "summary", "live", "narration"])
@pytest.mark.parametrize("value", ["0", "-1"])
async def test_generation_caps_fail_before_setup(
    monkeypatch: pytest.MonkeyPatch, entry: str, value: str
) -> None:
    import runpy

    from maf_cachebench import _live_cli

    option = "--response-max-tokens" if entry in {"replay", "advisor"} else "--answer-max-tokens"
    if entry == "narration":
        namespace = runpy.run_path(
            str(Path(__file__).resolve().parent.parent / "samples/probe_narration.py")
        )
        run = namespace["run"]
        globals_ = run.__globals__
        parser = namespace["build_parser"]()
    else:
        module, run = {
            "replay": (_cli, _cli.run_benchmark),
            "advisor": (_advise_cli, _advise_cli.run_advice),
            "summary": (_summary_cli, _summary_cli.run_summary),
            "recall": (_recall_cli, _recall_cli.run_recall),
            "live": (_live_cli, _live_cli.run_live_comparison),
        }[entry]
        globals_ = vars(module)
        parser = module.build_parser()

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Generation limits must fail before tokenizer/provider setup")

    monkeypatch.setitem(globals_, "build_provider", unexpected)
    monkeypatch.setitem(globals_, "build_tokenizer", unexpected)
    argv = [] if entry == "replay" else ["azure"]
    with pytest.raises(SystemExit, match=option):
        await run(parser.parse_args([*argv, option, value]))


@pytest.mark.parametrize(
    "option", ["--record-max-tokens", "--record-target-tokens", "--max-groups-before-record"]
)
async def test_optional_record_caps_reject_negative_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    from maf_cachebench import _live_cli

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid record caps must fail before setup")

    monkeypatch.setattr(_live_cli, "build_tokenizer", unexpected)
    with pytest.raises(SystemExit, match=option):
        await _live_cli.run_live_comparison(
            _live_cli.build_parser().parse_args(["azure", option, "-1"])
        )


@pytest.mark.parametrize(
    "entry,option",
    [
        ("replay", "--request-timeout"),
        ("advisor", "--request-timeout"),
        ("summary", "--request-timeout"),
        ("recall", "--request-timeout"),
        ("replay", "--turn-delay"),
        ("advisor", "--turn-delay"),
    ],
)
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
async def test_invalid_timing_fails_before_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str, option: str, value: str
) -> None:
    module, run = {
        "replay": (_cli, _cli.run_benchmark),
        "advisor": (_advise_cli, _advise_cli.run_advice),
        "summary": (_summary_cli, _summary_cli.run_summary),
        "recall": (_recall_cli, _recall_cli.run_recall),
    }[entry]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid timing must fail before setup")

    monkeypatch.setattr(module, "build_provider", unexpected)
    monkeypatch.setattr(module, "build_tokenizer", unexpected)
    argv = ["--out", str(tmp_path / "out")] if entry == "replay" else ["azure"]
    with pytest.raises(SystemExit, match=option):
        await run(module.build_parser().parse_args([*argv, f"{option}={value}"]))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("entry", ["replay", "advisor", "summary", "recall"])
@pytest.mark.parametrize("value", ["0", "0.01"])
async def test_zero_or_positive_cli_timing_is_valid(
    monkeypatch: pytest.MonkeyPatch, entry: str, value: str
) -> None:
    module, run = {
        "replay": (_cli, _cli.run_benchmark),
        "advisor": (_advise_cli, _advise_cli.run_advice),
        "summary": (_summary_cli, _summary_cli.run_summary),
        "recall": (_recall_cli, _recall_cli.run_recall),
    }[entry]

    class ReachedSetup(Exception):
        pass

    def setup(*args: Any, **kwargs: Any) -> None:
        raise ReachedSetup

    monkeypatch.setattr(module, "build_provider", setup)
    monkeypatch.setattr(module, "build_tokenizer", setup)
    argv = [] if entry == "replay" else ["azure"]
    if entry in {"advisor", "summary"}:
        argv += ["--price-input", "1"]
    with pytest.raises(ReachedSetup):
        await run(module.build_parser().parse_args([*argv, "--request-timeout", value]))
