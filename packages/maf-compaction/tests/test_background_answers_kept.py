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
    observe_record_merges,
    SessionCompactionRegistry,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
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

    def __init__(self):
        super().__init__()
        self.in_run = 0

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
            return response

        return answer()


@pytest.mark.parametrize("_attempt", range(10))
@pytest.mark.parametrize("roundtrip", [False, True], ids=["same session", "saved and loaded"])
async def test_no_accepted_background_merge_is_asked_again_in_a_later_run(roundtrip, _attempt):
    """Each run's last background answer used to be thrown away by its final reply, so the next
    run asked the same batch again; it is now applied by the end-of-run pass."""
    summarizer = _Summarizer()

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

    agent = background_harness(registry, _LoopModel(), lookup)
    events = []
    for run in range(RUNS):
        with observe_record_merges(lambda p, e, d: events.append((run, p, e, d))):
            await agent.run(f"Continue {run}.", session=session)
            work = registry._retained.get(session.session_id)
            if work:
                print("BOUNDARY", run, len(work._ready), work._task.done() if work._task else None,
                      work.expected_state == json.loads(json.dumps(session.state[registry.state_key])))
            if roundtrip:
                session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
    print("EVENTS", events)

    asked = Counter(summarizer.merges)
    assert asked, "the premise: the runs merged records"
    assert max(asked.values()) == 1, {body[:40]: n for body, n in asked.items() if n > 1}
