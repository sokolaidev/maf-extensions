"""Qualify idle retirement and retained retries with the native bounded-file helper."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

from scripts.experiments.mxc_files_patch.deletion_probe import LIMITS, SCRATCH, SEED
from scripts.experiments.mxc_files_patch.shared_call import call
from scripts.experiments.mxc_files_patch.transport import execute
from scripts.experiments.mxc_session_patch.durability_probe import _profile_for
from scripts.experiments.mxc_session_patch.host_call import atomic_report, digest
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.idle import Policy
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.shared_store import SECOND, SharedStore


class Clock:
    """Advance deterministic host time while executing real native workloads."""

    def __init__(self) -> None:
        self.value = 100 * SECOND

    def utc_ns(self) -> int:
        """Return controlled UTC."""
        return self.value

    def monotonic_ns(self) -> int:
        """Return matching elapsed time."""
        return self.value


def qualify(helper: Path, startup: Path, state: Path) -> dict:
    """Require active-call protection, restart-stable expiry and byte-identical retries."""
    clock = Clock()
    policy = Policy(10, 3)
    profile = _profile_for(helper, startup)
    root = state / "store"
    active = replace(SEED, code=SEED.code + b"\nfor _ in range(10000000): pass")
    with SharedStore(root, "one", profile, LIMITS, clock, idle_policy=policy) as store:
        seed = call(store, "seed", SEED, helper, startup, SCRATCH)
        assert store.begin("active", active.identity(), scratch=SCRATCH) is None
        journal = NativeJournal(store)
        work = journal.prepare("active")
        restored = work / "restored"
        assert store.restore(restored) == "seed"
        observations = 0
        children: list[subprocess.Popen[bytes]] = []

        def arm(child: subprocess.Popen[bytes]) -> None:
            journal.arm("active", child)
            children.append(child)

        def check() -> None:
            nonlocal observations
            if not (work / "native.ready").exists() or children[0].poll() is not None:
                return
            if observations == 0:
                clock.value += 20 * SECOND
            assert not journal.expire_idle()
            assert store._owner()["state"] == "active"
            observations += 1

        result = execute(
            helper,
            restored,
            work,
            active,
            restoring=True,
            before_start=arm,
            check_active=check,
            checkpoint_limits=(store.limits.checkpoint_bytes, store.limits.files),
        )
        assert observations > 0
        store.commit("active", work / "candidate", result)
        journal.reclaim("active")
        deadline = store.db.execute("SELECT deadline FROM session_idle").fetchone()[0]
        assert deadline == clock.value + 10 * SECOND
        clock.value += 9 * SECOND
        assert call(store, "seed", SEED, state / "absent", state / "absent", SCRATCH) == seed
        assert not journal.expire_idle()
        assert store.db.execute("SELECT deadline FROM session_idle").fetchone()[0] == deadline
    with SharedStore(root, "one", profile, LIMITS, clock, idle_policy=policy) as store:
        assert store.db.execute("SELECT deadline FROM session_idle").fetchone()[0] == deadline
        clock.value = deadline + 3 * SECOND
        assert NativeJournal(store).expire_idle()
        assert store.collect_checkpoints(limit=128) == 2
        assert call(store, "active", active, state / "absent", state / "absent", SCRATCH) == result
        assert call(store, "seed", SEED, state / "absent", state / "absent", SCRATCH) == seed
        try:
            call(store, "late", SEED, helper, startup, SCRATCH)
        except Refused as error:
            assert "retired" in str(error)
        else:
            raise AssertionError("idle session accepted new execution")
        assert store.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert not list((root / "scratch").iterdir())
        store.audit_usage()
    return {
        "status": "qualified",
        "helper_sha256": digest(helper),
        "controlled_clock": True,
        "running_call_protected": True,
        "idle_starts_after_cleanup": True,
        "retries_do_not_refresh_idle": True,
        "restart_preserves_deadline": True,
        "idle_retirement_blocks_execution": True,
        "checkpoints_collected": 2,
        "seed_retry_sha256": hashlib.sha256(seed).hexdigest(),
        "active_retry_sha256": hashlib.sha256(result).hexdigest(),
        "scratch_reclaimed": True,
    }


def main() -> int:
    """Write a report only after every native lifecycle assertion passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    atomic_report(
        state / "result.json", qualify(args.helper.resolve(), args.startup.resolve(), state)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
