"""Qualify bounded candidate writes and shared-store recovery on a real native helper."""

from __future__ import annotations

import argparse
import base64
import json
import platform
import subprocess
import sys
from pathlib import Path

from .host_call import digest, execute
from .host_store import CHUNK, MAX_CHECKPOINT, MAX_FILES, MAX_RESULT, Refused
from .native_journal import NativeJournal
from .shared_call import call
from .shared_store import PATH_BYTES, Limits, ScratchLimits, SharedStore


def console(result: bytes) -> bytes:
    """Decode the bounded native payload retained in the delivery record."""
    return base64.b64decode(json.loads(result)["console_base64"], validate=True)


def main() -> int:
    """Require native failure evidence and preserved committed state for every refusal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--seed-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    helper, startup, state = args.helper.resolve(), args.startup.resolve(), args.state_dir.resolve()
    if not args.seed_only:
        state.mkdir(parents=True, exist_ok=False)
    profile = {
        "helper": digest(helper),
        "startup_index": digest(startup / "index.json"),
        "platform": f"{platform.system()}-{platform.machine()}",
        "policy": "closed",
    }
    limits = Limits(8 * MAX_CHECKPOINT, 16 * MAX_CHECKPOINT)
    scratch = ScratchLimits(
        2 * MAX_CHECKPOINT + 5 * CHUNK,
        2 * MAX_FILES * (PATH_BYTES // 2 + 1) + 12,
        4 * MAX_CHECKPOINT + 10 * CHUNK,
    )
    first = b"mxc_stored_counter = 1; print(mxc_stored_counter)"
    measurements = {}
    if args.seed_only:
        with SharedStore(state / "store", "one", profile, limits) as db:
            saved = call(db, "seed", first, helper, startup, scratch, CHUNK)
            assert console(saved).strip() == b"1"
            (state / "seed-result.json").write_bytes(saved)
        return 0
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.experiments.mxc_session_patch.storage_probe",
            "--helper",
            str(helper),
            "--startup",
            str(startup),
            "--state-dir",
            str(state),
            "--seed-only",
        ],
        check=True,
        timeout=180,
    )
    with (state / "seed-result.json").open("rb") as stream:
        saved = stream.read(MAX_RESULT + 1)
    assert len(saved) <= MAX_RESULT and console(saved).strip() == b"1"
    with SharedStore(state / "store", "one", profile, limits) as db:
        assert (
            call(db, "seed", first, state / "missing", state / "missing", scratch, CHUNK) == saved
        )
        for name, bounds in (("bytes", (1, MAX_FILES)), ("files", (MAX_CHECKPOINT, 1))):
            code = b"mxc_stored_counter = 999"
            assert db.begin(name, code, scratch=scratch) is None
            journal = NativeJournal(db)
            work = journal.prepare(name)
            restored = work / "restored"
            assert db.restore(restored)
            try:
                execute(
                    helper,
                    restored,
                    work,
                    code,
                    CHUNK,
                    before_start=lambda child: journal.arm(name, child),
                    checkpoint_limits=bounds,
                )
            except Refused as error:
                assert "native execution/capture failed" in str(error), str(error)
            else:
                raise AssertionError("oversized native export succeeded")
            assert not (work / "native.json").exists()
            diagnostic = (work / "native.stderr").read_bytes()
            assert b"snapshot export exceeds reserved bounds" in diagnostic
            files = [path for path in (work / "candidate").rglob("*") if path.is_file()]
            written = sum(path.stat().st_size for path in files)
            assert written <= bounds[0] and len(files) <= bounds[1]
            assert (
                db.db.execute("SELECT current_call FROM sessions WHERE id='one'").fetchone()[0]
                == "seed"
            )
            measurements[name] = {
                "written_bytes": written,
                "written_files": len(files),
                "byte_limit": bounds[0],
                "file_limit": bounds[1],
            }
            journal.reclaim(name)
            try:
                db.begin(name, code, scratch=scratch)
            except Refused as error:
                assert "recovery" in str(error)
            else:
                raise AssertionError("interrupted call was admitted again")
        result = call(
            db,
            "next",
            b"assert mxc_stored_counter == 1; mxc_stored_counter += 1; print(mxc_stored_counter)",
            helper,
            startup,
            scratch,
            CHUNK,
        )
        assert console(result).strip() == b"2"
        assert db.collect_checkpoints() == 1
        assert (
            call(db, "seed", first, state / "missing", state / "missing", scratch, CHUNK) == saved
        )
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    assert not list((state / "store" / "scratch").iterdir())
    report = {
        "helper_sha256": digest(helper),
        "bounded_export": measurements,
        "same_machine_restart_replay": True,
        "failed_exports_preserve_checkpoint": True,
        "interrupted_calls_refuse_reexecution": True,
        "scratch_reclaimed": True,
        "collected_checkpoint_preserves_delivery": True,
        "max_result_bytes": MAX_RESULT,
    }
    (state / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
