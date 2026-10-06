"""Opt-in shared-store supervisor with bounded export and journaled scratch cleanup."""

from __future__ import annotations

from pathlib import Path

from .host_call import bounded_result_size, execute
from .host_store import CHUNK, Refused
from .native_journal import NativeJournal
from .shared_store import PATH_BYTES, ScratchLimits, SharedStore


def call(
    store: SharedStore,
    call_id: str,
    code: bytes,
    helper: Path,
    startup: Path,
    scratch: ScratchLimits,
    output_limit: int,
) -> bytes:
    """Publish before returning; failed calls retain reservations for explicit recovery."""
    if len(code) > 65536 or type(output_limit) is not int or not 0 < output_limit <= CHUNK:
        raise Refused("request or output limit exceeds the experiment's bounds")
    minimum_bytes = 2 * store.limits.checkpoint_bytes + 2 * CHUNK + output_limit + len(code) + 1024
    minimum_entries = 2 * store.limits.files * (PATH_BYTES // 2 + 1) + 12
    existing = store.db.execute(
        "SELECT 1 FROM calls WHERE session=? AND id=?", (store.session, call_id)
    ).fetchone()
    if existing is None and bounded_result_size(output_limit) > store.limits.result_bytes:
        raise Refused("maximum serialized result exceeds the result allowance")
    if existing is None and (scratch.bytes < minimum_bytes or scratch.entries < minimum_entries):
        raise Refused("scratch allowance cannot cover restore, bounded export and control files")
    saved = store.begin(call_id, code, scratch=scratch)
    if saved is not None:
        return saved
    journal = NativeJournal(store)
    work = journal.prepare(call_id)
    restored = work / "restored"
    base = restored if store.restore(restored) else startup
    result = execute(
        helper,
        base,
        work,
        code,
        output_limit,
        before_start=lambda child: journal.arm(call_id, child),
        checkpoint_limits=(store.limits.checkpoint_bytes, store.limits.files),
    )
    store.commit(call_id, work / "candidate", result)
    journal.reclaim(call_id)
    return result
