"""Qualify host publication against real exhaustion of a disposable small filesystem."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import physical
from .shared_store import Limits, Refused, SharedStore

MIB = 1024**2
POLICY = physical.Policy(32 * MIB, 4 * MIB, MIB)
LIMITS = Limits(64 * MIB, 128 * MIB, checkpoint_bytes=8 * MIB, result_bytes=128, files=2)
PROFILE = {"runtime": "host-storage-probe", "policy": "closed"}
RESULT = b"retained result"


def validate_volume(volume: Path, output: Path, token: str) -> None:
    """Require a marked, bounded filesystem separate from the checkout and reports."""
    if not token or (volume / ".mxc-volume-probe").read_text().strip() != token:
        raise ValueError("disposable volume ownership differs")
    if volume.is_symlink() or volume.is_junction():
        raise ValueError("volume path must not redirect")
    if not 64 * MIB <= shutil.disk_usage(volume).total <= 256 * MIB:
        raise ValueError("volume capacity outside disposable probe bounds")
    if any(volume.stat().st_dev == path.stat().st_dev for path in (output, Path.cwd())):
        raise ValueError("volume must be separate from reports and checkout")


def fill(volume: Path) -> dict[str, int]:
    """Allocate real bytes until the marked disposable volume rejects a write."""
    before = shutil.disk_usage(volume).free
    written = 0
    failure = None
    with (volume / "filler.bin").open("xb", buffering=0) as stream:
        block = os.urandom(MIB)
        for size in (MIB, 4096):
            while written <= 256 * MIB:
                try:
                    count = stream.write(block[:size])
                    if not count:
                        raise RuntimeError("filler write made no progress")
                    written += count
                    os.fsync(stream.fileno())
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    failure = error.errno
                    break
            else:
                raise RuntimeError("filler exceeded disposable capacity")
    after = shutil.disk_usage(volume).free
    if failure != errno.ENOSPC or after >= 4096 or before <= after:
        raise RuntimeError("filesystem exhaustion was not established")
    return {
        "before_free_bytes": before,
        "full_free_bytes": after,
        "written_bytes": written,
        "errno": errno.ENOSPC,
    }


def checkpoint(path: Path, content: bytes) -> Path:
    """Write one bounded candidate outside the exhausted filesystem."""
    path.mkdir()
    (path / "state").write_bytes(content)
    return path


def worker(volume: Path, output: Path, stage: str) -> dict[str, object]:
    """Run one store-owner lifetime and report only verified observations."""
    root = volume / "store"
    record: dict[str, object] = {"stage": stage, "pid": os.getpid()}

    class ExhaustingStore(SharedStore):
        def _capture(self, call_id: str, root: Path) -> int:
            # commit has already performed its real, transactional headroom check.
            record["exhaustion"] = fill(volume)
            record["capture_entered_in_transaction"] = self.db.in_transaction
            return super()._capture(call_id, root)

    if stage == "seed":
        with SharedStore(root, "one", PROFILE, LIMITS, physical_policy=POLICY) as db:
            db.begin("saved", b"saved")
            db.commit("saved", checkpoint(output / "seed", b"previous checkpoint"), RESULT)
            record["saved_result_sha256"] = hashlib.sha256(RESULT).hexdigest()
    elif stage == "overflow":
        candidate = checkpoint(output / "candidate", os.urandom(8 * MIB))
        with ExhaustingStore(root, "one", PROFILE, LIMITS, physical_policy=POLICY) as db:
            db.begin("failed", b"failed")
            try:
                db.commit("failed", candidate, b"must not be published")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
                record["sqlite_errorcode"] = error.sqlite_errorcode
            else:
                raise RuntimeError("publication unexpectedly succeeded on a full volume")
            if db.db.in_transaction:
                raise RuntimeError("failed publication left an active transaction")
            record["database_bytes"] = (root / "shared.sqlite").stat().st_size
            if record["database_bytes"] >= POLICY.database_bytes:
                raise RuntimeError("SQLite ceiling could explain the failure")
    elif stage == "recover":
        with SharedStore(root, "one", PROFILE, LIMITS, physical_policy=POLICY) as db:
            assert db.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.restore(output / "restored") == "saved"
            assert (output / "restored/state").read_bytes() == b"previous checkpoint"
            replay = db.begin("saved", b"saved")
            assert replay == RESULT
            row = db.db.execute("SELECT status,result FROM calls WHERE id='failed'").fetchone()
            assert tuple(row) == ("interrupted", None)
            assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
            try:
                db.begin("failed", b"failed")
            except Refused:
                pass
            else:
                raise RuntimeError("interrupted call was admitted again")
            db.audit_usage()
            record.update(
                previous_checkpoint_retained=True,
                reservation_retained=True,
                interrupted_retry_refused=True,
                integrity="ok",
                saved_result_sha256=hashlib.sha256(replay).hexdigest(),
            )
        # A separate session proves writes work again after capacity is restored.
        with SharedStore(root, "two", PROFILE, LIMITS, physical_policy=POLICY) as db:
            db.begin("new", b"new")
            db.commit("new", checkpoint(output / "new", b"recovered write"), b"ok")
            assert db.begin("new", b"new") == b"ok"
            db.audit_usage()
            record["new_publication_succeeded"] = True
    else:
        raise ValueError("unknown stage")
    return record


def launch(volume: Path, output: Path, token: str, stage: str) -> dict:
    """Require a successful fresh owner process and its completed report."""
    with (output / f"{stage}.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                __spec__.name,
                str(volume),
                str(output),
                token,
                "--stage",
                stage,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=90,
        )
    if completed.returncode:
        raise RuntimeError(f"{stage} failed; see stage log")
    return json.loads((output / f"{stage}.json").read_text())


def main() -> None:
    """Orchestrate exhaustion and recovery on an explicitly marked disposable volume."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("volume", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("token")
    parser.add_argument("--stage", choices=("seed", "overflow", "recover"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    validate_volume(args.volume, args.output, args.token)
    if args.stage:
        record = worker(args.volume, args.output, args.stage)
        (args.output / f"{args.stage}.json").write_text(json.dumps(record, indent=2) + "\n")
        return
    if (args.volume / "filler.bin").exists():
        raise ValueError("volume already contains a filler")
    report: dict[str, object] = {
        "qualified": False,
        "scope": "host store on disposable filesystem; no native guest",
        "source_sha": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "volume_bytes": shutil.disk_usage(args.volume).total,
        "policy": asdict(POLICY),
    }
    stages: dict[str, dict] = {}
    report["stages"] = stages
    try:
        stages["seed"] = launch(args.volume, args.output, args.token, "seed")
        try:
            stages["overflow"] = launch(args.volume, args.output, args.token, "overflow")
        finally:
            (args.volume / "filler.bin").unlink(missing_ok=True)
        stages["recover"] = launch(args.volume, args.output, args.token, "recover")
        assert stages["seed"]["saved_result_sha256"] == stages["recover"]["saved_result_sha256"]
        report["qualified"] = True
    finally:
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
