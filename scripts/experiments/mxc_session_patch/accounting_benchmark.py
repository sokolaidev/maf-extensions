"""Measure logical admission separately from full store auditing; no native guest runs."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .shared_store import CALL_METADATA, SESSION_METADATA, STORE_METADATA, Limits, SharedStore


class ScanningStore(SharedStore):
    """Control using the previous aggregate queries with identical trigger/write costs."""

    def usage(self, session: str | None = None) -> int:
        where = "" if session is None else " WHERE session=?"
        args = () if session is None else (session,)
        count = self.db.execute(
            "SELECT count(*) FROM sessions" + ("" if session is None else " WHERE id=?"), args
        ).fetchone()[0]
        retained = self.db.execute(
            "SELECT coalesce(sum(?+result_charge+checkpoint_charge),0) FROM calls" + where,
            (CALL_METADATA, *args),
        ).fetchone()[0]
        reserved = self.db.execute(
            "SELECT coalesce(sum(charge),0) FROM reservations" + where, args
        ).fetchone()[0]
        return (
            (STORE_METADATA if session is None else 0)
            + count * SESSION_METADATA
            + retained
            + reserved
        )


def measure(root: Path, history: int, samples: int, *, scanning: bool = False) -> dict[str, object]:
    """Seed retained interrupted identities; time successful admission including SQLite commit."""
    quota = STORE_METADATA + SESSION_METADATA + (history + samples + 10) * CALL_METADATA + 1_000_000
    limits = Limits(quota, quota, checkpoint_bytes=1024, result_bytes=128, files=2)
    cls = ScanningStore if scanning else SharedStore
    with cls(root, "one", {"runtime": "synthetic-no-guest"}, limits) as db:
        with db._transaction():
            db.db.executemany(
                "INSERT INTO calls(session,id,request,status,generation) VALUES('one',?,'seed','interrupted',1)",
                ((f"retained-{i}",) for i in range(history)),
            )
        db.audit_usage()
        original = db._transaction
        locks: list[float] = []
        elapsed: list[float] = []

        @contextmanager
        def timed() -> Iterator[None]:
            with original():
                acquired = time.perf_counter()
                yield
            locks.append(time.perf_counter() - acquired)

        for i in range(samples):
            db._transaction = timed
            started = time.perf_counter()
            db.begin(f"measured-{i}", b"code")
            elapsed.append(time.perf_counter() - started)
            db._transaction = original
            # Reconciliation is excluded from admission time; identities remain charged.
            with original():
                db.db.execute(
                    "UPDATE calls SET status='interrupted' WHERE id=?", (f"measured-{i}",)
                )
                db.db.execute("DELETE FROM reservations WHERE call=?", (f"measured-{i}",))
        started = time.perf_counter()
        db.audit_usage()
        audit_seconds = time.perf_counter() - started
        started = time.perf_counter()
        assert db.collect_checkpoints() == 0
        collection_seconds = time.perf_counter() - started
        return {
            "mode": "scan_control" if scanning else "stored_totals",
            "retained_identities_before": history,
            "retained_identities_after": history + samples,
            "samples": samples,
            "admission_median_seconds": statistics.median(elapsed),
            "admission_p95_seconds": sorted(elapsed)[(95 * samples - 1) // 100],
            "write_lock_median_seconds": statistics.median(locks),
            "write_lock_p95_seconds": sorted(locks)[(95 * samples - 1) // 100],
            "admissions_per_measured_second": samples / sum(elapsed),
            "accounting_audit_seconds": audit_seconds,
            "collection_zero_payload_seconds": collection_seconds,
        }


def main() -> None:
    """Write bounded, source-identified runner measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, nargs="+", default=[0, 1000, 10000])
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000 or any(not 0 <= n <= 100000 for n in args.history):
        parser.error("samples must be 1..1000 and histories 0..100000")
    results = []
    with TemporaryDirectory(prefix="mxc-accounting-") as temporary:
        for index, history in enumerate(args.history):
            for scanning in (False, True):
                results.append(
                    measure(
                        Path(temporary) / f"{index}-{scanning}",
                        history,
                        args.samples,
                        scanning=scanning,
                    )
                )
    report = {
        "source_sha": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "journal_mode": "DELETE",
        "synchronous": "FULL",
        "scope": "single-session, synthetic interrupted identities; no native execution, payloads, concurrent writers or physical power loss",
        "control": "previous aggregate lookups with current triggers; both modes retain new identities and exclude reconciliation from admission timing",
        "results": results,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
