"""Publish files and separate byte streams through the existing format-4 transaction and journal."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from scripts.experiments.mxc_files_patch.request import Request
from scripts.experiments.mxc_files_patch.transport import CONTROL_LIMIT, execute, result_limit
from scripts.experiments.mxc_session_patch.host_store import CHUNK, Refused
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.shared_store import (
    PATH_BYTES,
    ScratchLimits,
    SharedStore,
)


def call(
    store: SharedStore,
    call_id: str,
    request: Request,
    helper: Path,
    startup: Path,
    scratch: ScratchLimits,
    boundary: Callable[[str], None] = lambda _: None,
    *,
    delete: threading.Event | None = None,
) -> bytes:
    """Reserve file transfers before execution and publish before acknowledging success."""
    journal = NativeJournal(store)
    with journal.deletion(delete, boundary) as check:
        existing = store.db.execute(
            "SELECT 1 FROM calls WHERE session=? AND id=?", (store.session, call_id)
        ).fetchone()
        minimum_bytes = (
            2 * store.limits.checkpoint_bytes
            + 4 * CHUNK
            + len(request.code)
            + sum(len(item.data) for item in request.inputs)
            + request.limits.artifact_bytes
            + 3 * CONTROL_LIMIT
        )
        minimum_entries = 2 * store.limits.files * (PATH_BYTES // 2 + 1) + 24
        if existing is None and store.limits.result_bytes < result_limit(request):
            raise Refused("result allowance must cover encoded streams and artifacts")
        if existing is None and (
            scratch.bytes < minimum_bytes or scratch.entries < minimum_entries
        ):
            raise Refused("scratch allowance cannot cover files, streams and checkpoints")
        saved = store.begin(call_id, request.identity(), scratch=scratch)
        if saved is not None:
            return saved
        work = journal.prepare(call_id)
        restored = work / "restored"
        restoring = store.restore(restored) is not None
        base = restored if restoring else startup
        result = execute(
            helper,
            base,
            work,
            request,
            restoring=restoring,
            before_start=lambda child: journal.arm(call_id, child),
            check_active=check,
            checkpoint_limits=(store.limits.checkpoint_bytes, store.limits.files),
        )
        check()
        store.commit(call_id, work / "candidate", result, boundary)
        journal.reclaim(call_id, boundary)
        boundary("before_ack")
        return result
