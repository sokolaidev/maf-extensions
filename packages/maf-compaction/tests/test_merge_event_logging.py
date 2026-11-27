"""Merge events and pass mode reach the log as counts, so a host can see why merges stall."""

from __future__ import annotations

import logging

import pytest
from test_background_record_merges import prepared, running, tool_reply
from test_call_carryover import _budget, _compact, _Rebuilding, _transcript
from test_record_budget import _history, _strategy, _Summarizer

from maf_compaction import CompactionBudgetExceeded

pytestmark = pytest.mark.anyio

_LOGGER = "maf_compaction"


def _lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


async def test_a_finished_worker_logs_its_events_by_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy, work, messages = await prepared()
        await work.start(tool_reply())
        await running(work)

    (line,) = _lines(caplog, "Compaction background worker finished: merge events:")
    # survey and answer_prepared are sent only while an observer is attached; these always are.
    assert line == (
        "Compaction background worker finished: merge events: "
        "batch_selected=1 request_prepared=1 response_received=1."
    )
    await strategy(messages)
    await work.close()


async def test_a_reason_code_is_part_of_the_counted_name(caplog: pytest.LogCaptureFixture) -> None:
    strategy = _strategy(_Summarizer(), budget=100_000)
    for _ in range(2):
        strategy._merge_event("batch_skipped", reason="sources_not_durable")
    strategy._merge_event("batch_selected")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_merge_events("Probe")
        strategy._log_merge_events("Probe")

    first, second = _lines(caplog, "Probe:")
    assert first == "Probe: merge events: batch_selected=1 batch_skipped[sources_not_durable]=2."
    assert second == "Probe: merge events: none."


async def test_each_pass_says_how_it_came_by_its_list(caplog: pytest.LogCaptureFixture) -> None:
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.6), policy="shed")
    host = _Rebuilding(transcript)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for call in (1, 2):
            await _compact(strategy, host.hand())
            host.answered(call)

    first, second = _lines(caplog, "Compaction pass: mode=")
    assert first.startswith("Compaction pass: mode=full (nothing carried or saved); merge events:")
    assert second.startswith("Compaction pass: mode=carried view restored; merge events:")
    assert strategy.calls_resumed == 1


async def test_an_event_sent_between_passes_reaches_the_next_pass_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _strategy(_Summarizer(), budget=100_000)
    strategy._merge_event("worker_started", phase="background")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await strategy.compact_before(_history(2), options={})

    (line,) = _lines(caplog, "Compaction pass: mode=")
    assert "worker_started=1" in line


async def test_a_pass_that_fails_before_choosing_its_view_still_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = _strategy(_Summarizer(), budget=100_000)

    def over(options: object) -> int:
        raise CompactionBudgetExceeded(10, 1)

    monkeypatch.setattr(strategy, "sending_budget", over)
    with caplog.at_level(logging.INFO, logger=_LOGGER), pytest.raises(CompactionBudgetExceeded):
        await strategy.compact_before(_history(2), options={})

    (line,) = _lines(caplog, "Compaction pass: mode=")
    assert line.startswith("Compaction pass: mode=undecided; merge events:")
