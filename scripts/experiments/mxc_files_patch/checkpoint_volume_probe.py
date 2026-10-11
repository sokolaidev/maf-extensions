"""Qualify bounded-volume failures during native checkpoint restore and export."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.experiments.mxc_files_patch import physical_probe as native
from scripts.experiments.mxc_files_patch import transport
from scripts.experiments.mxc_files_patch.volume_probe import POLICY, VOLUME_BYTES
from scripts.experiments.mxc_session_patch import volume_probe as volume_control
from scripts.experiments.mxc_session_patch.host_call import atomic_report, digest
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.process_identity import Identity, stopped
from scripts.experiments.mxc_session_patch.shared_store import SharedStore

EXPORT_FREE = 32 * 1024**2
STAGES = ("seed", "fail", "recover")


def file_sizes(root: Path) -> dict[str, int]:
    """Measure only the probe's private scratch or restored checkpoint."""
    return {
        p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()
    }


def pressure(volume: Path, state: Path, release: int = 0) -> dict:
    """Exhaust the owned volume, optionally freeing a small native-export allowance."""
    evidence = volume_control.fill(volume, maximum_bytes=VOLUME_BYTES)
    if release:
        with (volume / "filler.bin").open("r+b") as stream:
            assert stream.seek(0, 2) > release
            stream.truncate(stream.tell() - release)
            stream.flush()
            os.fsync(stream.fileno())
        free = shutil.disk_usage(volume).free
        assert release <= free < release + 1024**2
        evidence["released_free_bytes"] = free
    atomic_report(state / "exhaustion.json", {**evidence})
    return evidence


def refusal(action, expected: str) -> None:
    """Require the specific conservative refusal rather than accepting any exception."""
    try:
        action()
    except Refused as error:
        assert expected in str(error), str(error)
    else:
        raise AssertionError("operation unexpectedly succeeded")


def worker(
    helper: Path,
    startup: Path,
    state: Path,
    volume: Path,
    mode: str,
    stage: str,
    prelaunch_recovery: bool = False,
) -> None:
    """Exercise one owner lifetime without weakening publication or recovery checks."""
    evidence: dict = {}

    class RestoreStore(SharedStore):
        def _file_bytes(self, file: sqlite3.Row, hashes: list[str]):
            for data in super()._file_bytes(file, hashes):
                if mode == "restore" and stage == "fail" and not evidence:
                    written = sum(file_sizes(self.root / "scratch").values())
                    if written:
                        evidence.update(pressure(volume, state))
                        evidence["partial_restore_bytes"] = written
                yield data

    with RestoreStore(
        volume / "store",
        f"copy-{mode}",
        native._profile_for(helper, startup),
        native.LIMITS,
        physical_policy=POLICY,
        prelaunch_recovery=prelaunch_recovery,
    ) as store:
        before = native.audit(store, POLICY)
        journal = NativeJournal(store)
        request = native.overflow((state / "payload.bin").read_bytes())
        if stage == "seed":
            result = native.call(store, "seed", native.SEED, helper, startup, native.SCRATCH)
            assert native.console(result).strip() == b"1"
            (state / "seed.bin").write_bytes(result)
            evidence["seed_sha256"] = hashlib.sha256(result).hexdigest()
        elif stage == "fail":
            assert store.begin("failed", request.identity(), scratch=native.SCRATCH) is None
            work = journal.prepare("failed")
            restored = work / "restored"
            if mode == "restore":
                try:
                    store.restore(restored)
                except OSError as error:
                    assert error.errno == errno.ENOSPC
                    evidence["restore_errno"] = error.errno
                else:
                    raise AssertionError("restore unexpectedly succeeded")
                assert evidence["partial_restore_bytes"] > 0
                assert not (work / "candidate").exists()
            else:
                assert store.restore(restored) == "seed"

                def before_start(child):
                    journal.arm("failed", child)
                    evidence.update(pressure(volume, state, EXPORT_FREE))

                refusal(
                    lambda: transport.execute(
                        helper,
                        restored,
                        work,
                        request,
                        restoring=True,
                        before_spawn=lambda: journal.start("failed"),
                        before_start=before_start,
                        checkpoint_limits=(store.limits.checkpoint_bytes, store.limits.files),
                    ),
                    "native call failed:",
                )
                diagnostic = (work / "native.stderr").read_text(encoding="utf-8", errors="replace")
                expected = 112 if sys.platform == "win32" else errno.ENOSPC
                assert re.search(rf"os error {expected}\b", diagnostic), diagnostic
                assert (work / "native.stdout.bin").read_bytes().strip() == b"999"
                assert (work / "candidate").is_dir()
                assert not (work / "candidate/index.json").exists()
                assert not (work / "native.json").exists()
                evidence.update(
                    export_os_error=expected,
                    native_execution_observed=True,
                    incomplete_candidate=True,
                    candidate_bytes=sum(file_sizes(work / "candidate").values()),
                )
            row = store.db.execute(
                "SELECT * FROM launches WHERE session=? AND call='failed'", (store.session,)
            ).fetchone()
            assert row is not None
            if mode == "restore":
                assert row["state"] == "prepared" and row["identity"] is None
                evidence["helper_unidentified"] = True
            else:
                assert row["state"] == "armed" and stopped(Identity.decode(row["identity"]))
                evidence["helper_stopped"] = True
            assert store._owner()["current_call"] == "seed"
            assert (
                store.db.execute(
                    "SELECT result FROM calls WHERE session=? AND id='failed'", (store.session,)
                ).fetchone()[0]
                is None
            )
            assert (
                store.db.execute(
                    "SELECT count(*) FROM reservations WHERE session=?", (store.session,)
                ).fetchone()[0]
                == 1
            )
            evidence["previous_checkpoint_retained"] = True
            evidence["reservation_retained"] = True
        else:
            assert stage == "recover" and store._owner()["current_call"] == "seed"
            assert (
                store.db.execute(
                    "SELECT status FROM calls WHERE session=? AND id='failed'", (store.session,)
                ).fetchone()[0]
                == "interrupted"
            )
            refusal(
                lambda: native.call(
                    store, "failed", request, state / "absent", state / "absent", native.SCRATCH
                ),
                "recovery",
            )
            if mode == "restore" and not prelaunch_recovery:
                row = store.db.execute(
                    "SELECT * FROM launches WHERE session=? AND call='failed'", (store.session,)
                ).fetchone()
                work = store.root / "scratch" / row["token"]
                retained = file_sizes(work)
                refusal(lambda: journal.reclaim("failed"), "unidentified")
                refusal(
                    lambda: native.call(
                        store,
                        "verify",
                        native.VERIFY,
                        state / "absent",
                        state / "absent",
                        native.SCRATCH,
                    ),
                    "outstanding reservation",
                )
                assert file_sizes(work) == retained
                assert (
                    store.db.execute(
                        "SELECT count(*) FROM reservations WHERE session=?", (store.session,)
                    ).fetchone()[0]
                    == 1
                )
                recovered = state / "restored"
                assert store.restore(recovered) == "seed"
                files = native.candidate_files(recovered)
                assert files["session/value"] == b"1" and "session/bulk.bin" not in files
                assert sum(retained.values()) < sum(file_sizes(recovered).values())
                evidence.update(
                    unidentified_reclaim_refused=True,
                    new_call_refused=True,
                    reservation_retained=True,
                    partial_scratch_retained=True,
                    checkpoint_files_verified=True,
                )
            else:
                journal.reclaim("failed")
                result = native.call(
                    store, "verify", native.VERIFY, helper, startup, native.SCRATCH
                )
                assert native.console(result).strip() == b"2"
                assert store.collect_checkpoints(limit=128) == 1
                assert (
                    store.db.execute(
                        "SELECT count(*) FROM launches WHERE session=?", (store.session,)
                    ).fetchone()[0]
                    == 0
                )
                assert not list((store.root / "scratch").iterdir())
                evidence.update(python_and_session_files_recovered=True, scratch_reclaimed=True)
            retry = native.call(
                store, "seed", native.SEED, state / "absent", state / "absent", native.SCRATCH
            )
            assert retry == (state / "seed.bin").read_bytes()
            evidence.update(
                interrupted_retry_refused=True, retry_sha256=hashlib.sha256(retry).hexdigest()
            )
        atomic_report(
            state / f"{stage}.json",
            {
                "pid": os.getpid(),
                "before": before,
                "after": native.audit(store, POLICY),
                **evidence,
            },
        )


def launch(
    helper: Path,
    startup: Path,
    state: Path,
    volume: Path,
    token: str,
    mode: str,
    stage: str,
    prelaunch_recovery: bool = False,
) -> dict:
    """Require an isolated owner process and its completed assertions."""
    with (state / f"{stage}.log").open("wb") as log:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.experiments.mxc_files_patch.checkpoint_volume_probe",
                "--helper",
                str(helper),
                "--startup",
                str(startup),
                "--state-dir",
                str(state),
                "--volume",
                str(volume),
                "--token",
                token,
                "--mode",
                mode,
                "--worker",
                stage,
                *(["--prelaunch-recovery"] if prelaunch_recovery else []),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    assert result.returncode == 0, (mode, stage, result.returncode)
    return json.loads((state / f"{stage}.json").read_bytes())


def qualify(
    helper: Path,
    startup: Path,
    state: Path,
    volume: Path,
    token: str,
    prelaunch_recovery: bool = False,
) -> dict:
    """Qualify export and restore independently under the selected recovery contract."""
    state.mkdir(parents=True, exist_ok=False)
    volume_control.validate_volume(volume, state, token, maximum_bytes=VOLUME_BYTES)
    filler = volume / "filler.bin"
    if filler.exists():
        raise ValueError("volume already contains a filler")
    cases: dict = {}
    report = {
        "qualified": False,
        "scope": "checkpoint copy recovery"
        if prelaunch_recovery
        else "checkpoint copy failure controls; restore cleanup remains blocked",
        "format": 7 if prelaunch_recovery else 6,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "helper_sha256": digest(helper),
        "platform": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "cases": cases,
    }
    try:
        for mode in ("export", "restore"):
            case = state / mode
            case.mkdir()
            (case / "payload.bin").write_bytes(os.urandom(native.PAYLOAD_BYTES))
            records = cases[mode] = {}
            records["seed"] = launch(
                helper, startup, case, volume, token, mode, "seed", prelaunch_recovery
            )
            try:
                records["fail"] = launch(
                    helper, startup, case, volume, token, mode, "fail", prelaunch_recovery
                )
            finally:
                volume_control.validate_volume(volume, state, token, maximum_bytes=VOLUME_BYTES)
                filler.unlink(missing_ok=True)
            records["recover"] = launch(
                helper, startup, case, volume, token, mode, "recover", prelaunch_recovery
            )
            assert records["seed"]["seed_sha256"] == records["recover"]["retry_sha256"]
            assert len({r["pid"] for r in records.values()}) == 3
        report["qualified"] = True
    finally:
        atomic_report(state / "result.json", report)
    return report


def main() -> int:
    """Run the checkpoint-copy matrix on an owned disposable filesystem."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("helper", "startup", "state-dir", "volume"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--mode", choices=("restore", "export"))
    parser.add_argument("--worker", choices=STAGES)
    parser.add_argument("--prelaunch-recovery", action="store_true")
    args = parser.parse_args()
    if args.worker:
        if args.mode is None:
            parser.error("worker requires mode")
        volume_control.validate_volume(
            args.volume, args.state_dir, args.token, maximum_bytes=VOLUME_BYTES
        )
        worker(
            args.helper,
            args.startup,
            args.state_dir,
            args.volume,
            args.mode,
            args.worker,
            args.prelaunch_recovery,
        )
    else:
        qualify(
            args.helper,
            args.startup,
            args.state_dir,
            args.volume,
            args.token,
            args.prelaunch_recovery,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
