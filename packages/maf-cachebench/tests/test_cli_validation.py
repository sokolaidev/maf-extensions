"""CLI selections and offline reports fail honestly before provider work."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from maf_cachebench import _advise_cli, _cli, _recall_cli, _summary_cli
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
