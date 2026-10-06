"""Sample arguments are validated without contacting providers."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("repeats", ["0", "-1"])
async def test_narration_rejects_nonpositive_repeats_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    repeats: str,
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid repeats must fail before provider setup")

    run = namespace["run"]
    monkeypatch.setitem(run.__globals__, "build_provider", unexpected)
    args = namespace["build_parser"]().parse_args(["azure", "--repeats", repeats])
    with pytest.raises(SystemExit, match="--repeats must be greater than 0"):
        await run(args)


async def test_narration_without_completed_samples_is_not_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    run = namespace["run"]

    async def empty(*args: Any, **kwargs: Any) -> Any:
        return [], [], ""

    monkeypatch.setitem(
        run.__globals__,
        "build_provider",
        lambda *a, **k: SimpleNamespace(client=object(), model="stub"),
    )
    monkeypatch.setitem(run.__globals__, "_measure", empty)
    args = namespace["build_parser"]().parse_args(
        [
            "azure",
            "--repeats",
            "1",
            "--narrations",
            "neutral",
            "--placements",
            "head",
        ]
    )
    assert await run(args) == 1


@pytest.mark.parametrize(
    "argv,model,openai",
    [
        ([], "glm-5.2:cloud", False),
        (["--openai"], "glm-5.2:cloud", True),
        (["--openai", "test-model"], "test-model", True),
        (["test-model", "--openai"], "test-model", True),
        (["test-model"], "test-model", False),
    ],
)
def test_ollama_surface_flag_never_becomes_the_model(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    model: str,
    openai: bool,
) -> None:
    sent: list[tuple[str, str]] = []

    def post(url: str, *, json: dict[str, Any], **kwargs: Any) -> httpx.Response:
        sent.append((url, json["model"]))
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    monkeypatch.setattr(sys, "argv", ["probe_ollama_usage.py", *argv])
    monkeypatch.setattr(httpx, "post", post)
    namespace = runpy.run_path(str(SAMPLES / "probe_ollama_usage.py"))
    assert namespace["main"]() == 0
    suffix = "/v1/chat/completions" if openai else "/api/chat"
    assert len(sent) == 2
    assert all(url.endswith(suffix) and selected == model for url, selected in sent)


@pytest.mark.parametrize(
    "option,selection", [("--narrations", "neutral,neutral"), ("--placements", "head,head")]
)
async def test_narration_duplicates_fail_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    selection: str,
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    run = namespace["run"]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Duplicate configurations must fail before setup")

    monkeypatch.setitem(run.__globals__, "build_provider", unexpected)
    with pytest.raises(SystemExit, match="Duplicate"):
        await run(namespace["build_parser"]().parse_args(["azure", option, selection]))


@pytest.mark.parametrize(
    "option,value",
    [
        ("--tool-turns", "2"),
        ("--markers-per-tool", "0"),
        ("--filler-turns", "-1"),
        ("--filler-tokens", "-1"),
    ],
)
async def test_narration_rejects_clamped_workload_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    run = namespace["run"]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid workload must fail before provider setup")

    monkeypatch.setitem(run.__globals__, "build_provider", unexpected)
    with pytest.raises(SystemExit, match=option):
        await run(namespace["build_parser"]().parse_args(["azure", option, value]))


@pytest.mark.parametrize("entry", ["replay", "advisor", "sample"])
@pytest.mark.parametrize("cached", [None, 0, 50])
async def test_replay_requires_every_turn_cache_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str, cached: int | None
) -> None:
    from maf_cachebench import ProviderRuntime, TurnRecord, _advise_cli, _cli, summarize_cell

    if entry == "sample":
        namespace = runpy.run_path(str(SAMPLES / "run_benchmark.py"))["main"].__globals__
    else:
        namespace = vars(_cli if entry == "replay" else _advise_cli)
    monkeypatch.setitem(namespace, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub"))
    observed: list[Any] = []

    async def records(**kwargs: Any) -> list[TurnRecord]:
        return [
            TurnRecord(
                cell=kwargs["cell"],
                turn=index,
                history_messages=2,
                sent_messages=2,
                sent_tokens_local=100,
                reusable_prefix_tokens_local=50,
                prefix_broken=False,
                input_tokens=100,
                cached_tokens=value,
                output_tokens=1,
                latency_ms=1,
            )
            for index, value in enumerate((40, cached), start=1)
        ]

    def summarize(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["reports_cache_tokens"] is (cached is not None)
        result = summarize_cell(*args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setitem(namespace, "run_cell", records)
    monkeypatch.setitem(namespace, "summarize_cell", summarize)
    if entry == "sample":
        await namespace["main"]()
    elif entry == "replay":
        args = _cli.build_parser().parse_args(
            [
                "--providers",
                "azure",
                "--strategies",
                "none",
                "--sizes",
                "small",
                "--out",
                str(tmp_path),
                "--tokenizer",
                "estimator",
            ]
        )
        assert await _cli.run_benchmark(args) == 0
    else:
        args = _advise_cli.build_parser().parse_args(
            [
                "azure",
                "--strategies",
                "none",
                "--repeats",
                "1",
                "--tokenizer",
                "estimator",
            ]
        )
        await _advise_cli._measure(args, "azure", ProviderRuntime(None, "stub"))
    assert observed
    assert all((cell.cache_hit_ratio is not None) is (cached is not None) for cell in observed)


async def test_benchmark_sample_isolates_executions(monkeypatch: pytest.MonkeyPatch) -> None:
    from maf_cachebench import ProviderRuntime

    main = runpy.run_path(str(SAMPLES / "run_benchmark.py"))["main"]
    namespace = main.__globals__
    monkeypatch.setitem(namespace, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub"))
    monkeypatch.setitem(namespace, "PROVIDER", "mistral")
    monkeypatch.setitem(
        namespace, "prompt_cache_key_options", lambda provider, salt: {"prompt_cache_key": salt}
    )
    salts: list[str] = []
    keys: list[Any] = []
    original = namespace["build_preset"]

    def preset(*args: Any, **kwargs: Any) -> Any:
        salts.append(kwargs["salt"])
        return original(*args, **kwargs)

    def caller(*args: Any, **kwargs: Any) -> None:
        keys.append(kwargs.get("extra_options"))

    async def records(**kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setitem(namespace, "build_preset", preset)
    monkeypatch.setitem(namespace, "ProviderCaller", caller)
    monkeypatch.setitem(namespace, "run_cell", records)
    await main()
    await main()
    assert len(salts) == len(set(salts)) == 4
    assert len({str(key) for key in keys}) == 4
    assert "none" in salts[0]
    assert "context_window" in salts[1]


async def test_cache_stability_sample_isolates_executions(monkeypatch: pytest.MonkeyPatch) -> None:
    from maf_cachebench import CallOutcome, ProviderRuntime

    namespace = runpy.run_path(str(SAMPLES / "probe_cache_stability.py"))
    run = namespace["run"]
    monkeypatch.setitem(
        run.__globals__, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub")
    )
    prompts: list[list[str]] = []
    keys: list[Any] = []

    def caller(*args: Any, **kwargs: Any) -> Any:
        seen: list[str] = []
        prompts.append(seen)
        keys.append(kwargs["extra_options"])

        async def respond(messages: Any) -> CallOutcome:
            seen.append(str(messages[0].text))
            return CallOutcome(input_tokens=100, cached_tokens=50, latency_ms=1)

        return respond

    monkeypatch.setitem(run.__globals__, "ProviderCaller", caller)
    monkeypatch.setitem(
        run.__globals__,
        "prompt_cache_key_options",
        lambda provider, salt: {"prompt_cache_key": salt},
    )
    args = namespace["build_parser"]().parse_args(["mistral", "--calls", "3"])
    assert await run(args) == await run(args) == 0
    assert all(len(set(prompt)) == 1 and len(prompt) == 3 for prompt in prompts)
    assert prompts[0][0] != prompts[1][0]
    assert keys[0] != keys[1]


async def test_narration_sample_isolates_executions(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_framework import CharacterEstimatorTokenizer

    from maf_cachebench import ProviderRuntime

    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    measure = namespace["_measure"]
    globals_ = measure.__globals__
    monkeypatch.setitem(globals_, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub"))
    monkeypatch.setitem(globals_, "build_tokenizer", lambda *a: CharacterEstimatorTokenizer())
    salts: list[str] = []

    def scenario(**kwargs: Any) -> Any:
        salts.append(kwargs["salt"])
        return SimpleNamespace()

    async def live(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(error=None)

    monkeypatch.setitem(globals_, "build_live_scenario", scenario)
    monkeypatch.setitem(globals_, "run_live", live)
    monkeypatch.setitem(globals_, "score_samples", lambda *a: [])
    args = namespace["build_parser"]().parse_args(["azure", "--repeats", "2"])
    await measure(args, "neutral", "head")
    await measure(args, "neutral", "head")
    assert len(salts) == len(set(salts)) == 4
    assert all("neutral-head" in salt for salt in salts)


def test_mistral_sample_isolates_executions(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def post(url: str, *, json: dict[str, Any], **kwargs: Any) -> httpx.Response:
        sent.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(sys, "argv", ["probe_mistral_cache.py"])
    namespace = runpy.run_path(str(SAMPLES / "probe_mistral_cache.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "API_KEY", "stub")
    assert main() == main() == 0
    prefixes = [request["messages"][0]["content"] for request in sent]
    assert len(prefixes) == 12
    assert len(set(prefixes)) == 4
    assert all(len(set(prefixes[index : index + 3])) == 1 for index in range(0, 12, 3))
    assert sent[3]["prompt_cache_key"] != sent[9]["prompt_cache_key"]


@pytest.mark.parametrize(
    "reports,steady",
    [
        ([50], False),
        ([0, 50], False),
        ([0, 50, 50], True),
        ([0, None, 50], False),
        ([0, -1, 50], False),
        ([0, None, -1, 50, 50], True),
    ],
)
async def test_stability_requires_warm_observations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reports: list[int | None],
    steady: bool,
) -> None:
    from maf_cachebench import CallOutcome, ProviderRuntime

    namespace = runpy.run_path(str(SAMPLES / "probe_cache_stability.py"))
    run = namespace["run"]
    monkeypatch.setitem(
        run.__globals__, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub")
    )
    call_count = max(3, len(reports))
    pending = iter([*reports, *([None] * (call_count - len(reports)))])

    async def respond(messages: Any) -> CallOutcome:
        cached = next(pending)
        if cached == -1:
            return CallOutcome(error="unavailable", latency_ms=1)
        return CallOutcome(input_tokens=100, cached_tokens=cached, latency_ms=1)

    monkeypatch.setitem(run.__globals__, "ProviderCaller", lambda *a, **k: respond)
    args = namespace["build_parser"]().parse_args(["mistral", "--calls", str(call_count)])
    assert await run(args) == 0
    output = capsys.readouterr().out
    assert ("\nSTEADY:" in output) is steady
    assert ("Not enough usable calls" in output) is not steady
    if steady:
        assert "warm calls (excluding the first): 2" in output


@pytest.mark.parametrize(
    "option,value",
    [
        ("--calls", "-1"),
        ("--calls", "0"),
        ("--calls", "1"),
        ("--calls", "2"),
        ("--prompt-tokens", "0"),
        ("--prompt-tokens", "-1"),
    ],
)
async def test_stability_invalid_configuration_fails_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_cache_stability.py"))
    run = namespace["run"]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid measurements must fail before provider setup")

    monkeypatch.setitem(run.__globals__, "build_provider", unexpected)
    args = namespace["build_parser"]().parse_args(["mistral", option, value])
    with pytest.raises(SystemExit, match=option):
        await run(args)


@pytest.mark.parametrize("first_error", [False, True])
@pytest.mark.parametrize("warm_hits", [[50, 50], [10, 50, 50]])
async def test_stability_keeps_all_warm_calls_after_unreported_first_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    first_error: bool,
    warm_hits: list[int],
) -> None:
    from maf_cachebench import CallOutcome, ProviderRuntime

    namespace = runpy.run_path(str(SAMPLES / "probe_cache_stability.py"))
    run = namespace["run"]
    outcomes = iter(
        [
            CallOutcome(
                latency_ms=1, input_tokens=100, error="unavailable" if first_error else None
            ),
            *(CallOutcome(latency_ms=1, input_tokens=100, cached_tokens=hit) for hit in warm_hits),
        ]
    )

    async def respond(messages: Any) -> CallOutcome:
        return next(outcomes)

    monkeypatch.setitem(
        run.__globals__, "build_provider", lambda *a, **k: ProviderRuntime(None, "stub")
    )
    monkeypatch.setitem(run.__globals__, "ProviderCaller", lambda *a, **k: respond)
    args = namespace["build_parser"]().parse_args(["mistral", "--calls", str(len(warm_hits) + 1)])
    assert await run(args) == 0
    output = capsys.readouterr().out
    assert f"warm calls (excluding the first): {len(warm_hits)}" in output
    assert ("INTERMITTENT:" in output) is (10 in warm_hits)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--narrations", ""),
        ("--narrations", ", ,"),
        ("--narrations", "unknown"),
        ("--placements", ""),
        ("--placements", ", ,"),
        ("--placements", "unknown"),
        ("--agent", "unknown"),
    ],
)
async def test_narration_invalid_modes_fail_before_setup(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    run = namespace["run"]

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid modes must fail before provider/tokenizer setup")

    for name in ("build_provider", "build_tokenizer"):
        monkeypatch.setitem(run.__globals__, name, unexpected)
    args = namespace["build_parser"]().parse_args(["azure", option, value])
    with pytest.raises(SystemExit, match=option):
        await run(args)
