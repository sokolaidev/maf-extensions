"""Detached behavioral snapshots for independent probes and request retries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from maf_compaction import RecallGate, ToolResultRecallMiddleware


@dataclass(frozen=True)
class BehaviorSnapshot:
    """Behavioral state, excluding cumulative measurement counters where the API permits."""

    strategy: Any
    session_state: bool
    recall: dict[str, Any] | None
    gate: dict[str, Any] | None
    encoded_decisions: bool = False


def snapshot_behavior(strategy: Any, recall: ToolResultRecallMiddleware | None) -> BehaviorSnapshot:
    """Copy every public behavioral surface supported by the installed strategy."""
    take = getattr(strategy, "session_state", None)
    session_state = callable(take)
    if not session_state:
        take = getattr(strategy, "decision_state", None)
    state = take() if callable(take) else None
    serialize = getattr(state, "to_dict", None)
    encoded = not session_state and callable(serialize)
    if encoded and callable(serialize):
        state = serialize()
    gate = getattr(recall.arm, "__self__", None) if recall is not None else None
    return BehaviorSnapshot(
        deepcopy(state),
        session_state,
        json.loads(json.dumps(recall.session_state())) if recall is not None else None,
        gate.session_state() if isinstance(gate, RecallGate) else None,
        encoded,
    )


def restore_behavior(
    strategy: Any, recall: ToolResultRecallMiddleware | None, snapshot: BehaviorSnapshot
) -> None:
    """Restore a snapshot into either the original objects or a freshly constructed stack."""
    if snapshot.strategy is not None:
        state = deepcopy(snapshot.strategy)
        if snapshot.session_state:
            strategy.restore_session_state(state)
        elif snapshot.encoded_decisions:
            strategy.restore_decisions(cast(Any, type(strategy.decision_state())).from_dict(state))
        else:
            strategy.restore_decisions(state)
    if recall is not None and snapshot.recall is not None:
        recall.forget_pending()
        recall.restore_session_state(json.loads(json.dumps(snapshot.recall)))
        gate = getattr(recall.arm, "__self__", None)
        if isinstance(gate, RecallGate) and snapshot.gate is not None:
            gate.restore_session_state(dict(snapshot.gate))
