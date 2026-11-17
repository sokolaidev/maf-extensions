"""Publish files and separate byte streams through the shared-store transaction and journal."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from maf_sandbox_mxc.files.request import Request
from maf_sandbox_mxc.files.transport import CONTROL_LIMIT, execute, result_limit
from maf_sandbox_mxc.host_store import CHUNK, Refused
from maf_sandbox_mxc.native_journal import NativeJournal
from maf_sandbox_mxc.shared_store import (
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
    cancel: threading.Event | None = None,
    timeout: float = 90,
    restore_state: bool = True,
    validate_result: Callable[[bytes], None] = lambda _: None,
) -> bytes:
    """Reserve file transfers before execution and publish before acknowledging success."""
    journal = NativeJournal(store)
    with journal.deletion(delete, boundary) as check:

        def active() -> None:
            check()
            if cancel is not None and cancel.is_set():
                raise Refused("call cancelled")

        active()
        if not 0 < timeout <= 90:
            raise Refused("invalid native deadline")
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
        restoring = restore_state and store.restore(restored) is not None
        base = restored if restoring else startup
        try:
            result = execute(
                helper,
                base,
                work,
                request,
                restoring=restoring,
                before_spawn=lambda: journal.start(call_id, boundary),
                before_start=lambda child: journal.arm(call_id, child, boundary),
                check_active=active,
                cancel=cancel,
                timeout=timeout,
                checkpoint_limits=(store.limits.checkpoint_bytes, store.limits.files),
            )
            active()
            validate_result(result)
            store.commit(call_id, work / "candidate", result, boundary)
            journal.reclaim(call_id, boundary)
            boundary("before_ack")
            active()
            return result
        except BaseException:
            row = store.db.execute(
                "SELECT identity FROM launches WHERE session=? AND call=?",
                (store.session, call_id),
            ).fetchone()
            if row is not None and row["identity"] is not None:
                journal.reclaim(call_id, boundary)
            raise
