"""A background merge answer that is ready when the run ends is applied, not asked again."""

import asyncio
import json
import logging
from collections import Counter

import pytest
from agent_framework import (
    AgentSession,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    tool,
)
from test_background_record_merges import background_harness, prepared, running, tool_reply
from test_record_budget import _history, _Summarizer
from test_session import Model, build_stack

from maf_compaction import (
    SessionCompactionRegistry,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    observe_record_merges,
)

pytestmark = pytest.mark.anyio

#: Tool rounds per run before the run's final reply.
ROUNDS = 3
RUNS = 4
RECORDS = 12
CAP = 3


def final_reply() -> list[Message]:
    return [Message("assistant", ["done"], message_id="final")]


async def test_a_reply_without_tool_calls_keeps_what_is_ready_for_the_next_pass():
    strategy, work, messages = await prepared()
    await work.start(tool_reply())
    await running(work)
    assert len(work._ready) == 1, "the premise: one answer is ready"

    await work.start(final_reply())

    assert len(work._ready) == 1, "the run's last reply keeps it"
    assert not work.defer_merges, "with nothing running, the foreground may merge again"
    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    await work.close()


async def test_worker_finishing_during_application_commits_both_batches(monkeypatch):
    second_request = asyncio.Event()
    applying = asyncio.Event()

    class Summarizer(_Summarizer):
        async def get_response(self, messages, **kwargs):
            if len(self.merges) == 1:
                second_request.set()
                await applying.wait()
            return await super().get_response(messages, **kwargs)

    model = Summarizer()
    strategy, work, messages = await prepared(model)
    strategy.record_merge_max_records = 2
    try:
        await work.start(tool_reply())
        worker = running(work)
        await asyncio.wait_for(second_request.wait(), timeout=10)
        assert len(work._ready) == 1
        apply = strategy._apply_record_answers

        async def apply_after_worker_finishes(history, answers):
            applying.set()
            await worker
            return await apply(history, answers)

        monkeypatch.setattr(strategy, "_apply_record_answers", apply_after_worker_finishes)
        assert await work.apply_ready(messages)
        assert strategy.records_merged == len(model.merges) == 2
        assert not work._ready
    finally:
        await work.close()


async def test_closing_with_work_unapplied_says_so_in_the_log(caplog):
    _, work, _ = await prepared()
    await work.start(tool_reply())
    await running(work)
    with caplog.at_level(logging.INFO, logger="maf_compaction._composed"):
        await work.close()
    assert "Background record merge closed with work unapplied: discarded=1 pending=0" in (
        caplog.text
    )


async def test_a_discarded_ready_answer_says_so_in_the_log(caplog):
    strategy, work, messages = await prepared()
    await work.start(tool_reply())
    await running(work)
    messages[3].contents = [Content.from_text("changed under the worker")]
    with caplog.at_level(logging.INFO, logger="maf_compaction._composed"):
        assert not await work.apply_ready(messages)
    assert "discarded a ready background merge of" in caplog.text
    assert "sources_unavailable" in caplog.text
    assert strategy.records_merged == 0
    await work.close()


class _LoopModel(Model):
    """ROUNDS tool calls a run, then a text reply; a pinned recall is answered as Model does."""

    def __init__(self, before_final_reply):
        super().__init__()
        self.in_run = 0
        self.before_final_reply = before_final_reply

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        choice = options.get("tool_choice")
        if isinstance(choice, dict) and choice.get("required_function_name"):
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )
        self.in_run += 1
        if self.in_run <= ROUNDS:
            contents = [
                Content.from_function_call(
                    call_id=f"lookup-{len(self.prompts)}-{self.in_run}", name="lookup", arguments={}
                )
            ]
        else:
            self.in_run = 0
            contents = [Content.from_text("answer")]
        self.prompts.append(len(messages))
        response = ChatResponse(
            messages=Message("assistant", contents, message_id=f"reply-{len(self.prompts)}")
        )
        if stream:

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=contents)

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer():
            if self.in_run == 0:
                await self.before_final_reply()
            return response

        return answer()


@pytest.mark.parametrize("roundtrip", [False, True], ids=["same session", "saved and loaded"])
async def test_no_accepted_background_merge_is_asked_again_in_a_later_run(roundtrip):
    """Answers ready at the final reply survive later runs, including a session round trip."""
    release = asyncio.Event()
    ready_at_final = 0

    class Summarizer(_Summarizer):
        async def get_response(self, messages, **kwargs):
            await release.wait()
            return await super().get_response(messages, **kwargs)

    summarizer = Summarizer()

    def build():
        stack = build_stack(summarizer, budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        stack.strategy.record_merge_max_records = CAP
        return stack

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(RECORDS)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        return "lookup complete"

    async def finish_background():
        nonlocal ready_at_final
        work = registry._merge_work[session.session_id][1]
        release.set()
        if work._task is not None:
            await asyncio.wait_for(running(work), timeout=10)
        ready_at_final += len(work._ready)

    agent = background_harness(registry, _LoopModel(finish_background), lookup)
    events = []
    with observe_record_merges(
        lambda phase, event, details: events.append((phase, event, details))
    ):
        for run in range(RUNS):
            release.clear()
            await asyncio.wait_for(agent.run(f"Continue {run}.", session=session), timeout=10)
            if roundtrip:
                session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))

    assert ready_at_final, "the premise: answers were ready before a final reply"
    prepared_batches = Counter(d["batch"] for _, e, d in events if e == "answer_prepared")
    accepted_batches = Counter(
        d["batch"] for p, e, d in events if p == "apply" and e == "answer_accepted"
    )
    assert accepted_batches == prepared_batches
    assert sum(accepted_batches.values()) == len(summarizer.merges)
    asked = Counter(summarizer.merges)
    assert asked, "the premise: the runs merged records"
    assert max(asked.values()) == 1, {body[:40]: n for body, n in asked.items() if n > 1}
