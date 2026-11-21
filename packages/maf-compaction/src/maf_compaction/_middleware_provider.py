"""Place compaction middleware inside per-service-call history persistence."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from agent_framework import (
    AgentSession,
    ChatContext,
    ChatMiddleware,
    ContextProvider,
    Message,
    SessionContext,
)

from ._request import attribute_compaction_context


class _CompactionContextMiddleware(ChatMiddleware):
    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        strategy = context.kwargs.get(
            "compaction_strategy", getattr(context.client, "compaction_strategy", None)
        )
        if strategy is not None:

            async def compact(messages: list[Message]) -> bool:
                original_contents = {id(message.contents): message.contents for message in messages}
                try:
                    return await cast(Callable[[list[Message]], Awaitable[bool]], strategy)(
                        messages
                    )
                finally:
                    attribute_compaction_context(messages, original_contents)

            context.kwargs = {**context.kwargs, "compaction_strategy": compact}
        await call_next()


class CompactionMiddlewareProvider(ContextProvider):
    """Run recall and request observers after history loading, before persistence.

    Install this provider on the agent instead of installing its chat middleware
    directly. Recall retries then reuse one loaded request and persist only the
    accepted response. Middleware order is preserved.
    """

    def __init__(
        self,
        middleware: Sequence[ChatMiddleware],
        *,
        source_id: str = "maf_compaction_middleware",
    ) -> None:
        super().__init__(source_id)
        self.middleware = list(middleware)

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        context.extend_middleware(
            self.source_id, [*self.middleware, _CompactionContextMiddleware()]
        )
