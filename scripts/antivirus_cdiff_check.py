"""Run offline native CDIFF regressions inside the candidate image."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch


def check(guest: Any, fixtures: Path) -> list[str]:
    """Exercise signed chains through preparation, including the bytecode scan path."""
    original_header = guest.header
    original_checked = guest.checked
    original_run = guest.run
    scans: list[tuple[list[str], int, str]] = []
    applied = False

    def observed_run(
        command: list[str], deadline: float, *, cwd: Path | None = None
    ) -> tuple[int, str]:
        code, output = original_run(command, deadline, cwd=cwd)
        if command[0] == "clamscan":
            scans.append((command, code, output))
        return code, output

    def fixture_header(path: Path) -> dict[str, int]:
        # Upstream signs test.info, so only its lookup name differs from production.
        metadata = original_header(path.with_name("test.info") if path.suffix == ".info" else path)
        if (
            case == "wrong-version"
            and applied
            and path.suffix == ".info"
            and path.parent.name == name
        ):
            metadata = metadata | {"version": metadata["version"] + 1}
        return metadata

    def fixture_checked(command: list[str], deadline: float, *, cwd: Path | None = None) -> None:
        nonlocal applied
        is_patch = command[1].startswith("--run-cdiff=")
        if is_patch:
            source = Path(command[1].split("=", 1)[1])
            alias = source.with_name("test-" + source.name.split("-", 1)[1])
            shutil.copyfile(source, alias)
            command = [command[0], f"--run-cdiff={alias}"]
        original_checked(command, deadline, cwd=cwd)
        if is_patch:
            applied = True

    reports = []
    for name in guest.DATABASES:
        for case in ("valid-chain", "tampered-signature", "wrong-version"):
            applied = False
            with tempfile.TemporaryDirectory() as directory:
                previous = Path.cwd()
                os.chdir(directory)
                try:
                    seed = Path(directory) / "seed"
                    seed.mkdir()
                    for database in guest.DATABASES:
                        shutil.copyfile(fixtures / "test-1.cvd", seed / f"{database}.cvd")
                    records = []
                    for index, version in enumerate((2, 3)):
                        data = (fixtures / f"test-{version}.cdiff").read_bytes()
                        if case == "tampered-signature" and version == 2:
                            signature = data.rfind(b":") + 1
                            data = (
                                data[:signature]
                                + (b"B" if data[signature : signature + 1] == b"A" else b"A")
                                + data[signature + 1 :]
                            )
                        Path(f"update-{index}-0.bin").write_bytes(data)
                        records.append(
                            {
                                "name": f"{name}-{version}.cdiff",
                                "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                            }
                        )
                    with (
                        patch.object(guest, "SEED", seed),
                        patch.object(guest, "header", fixture_header),
                        patch.object(guest, "checked", fixture_checked),
                        patch.object(guest, "run", observed_run),
                    ):
                        request = {
                            "now": 1624408809 + 8 * 86400,
                            "max_age_seconds": 14 * 86400,
                            "update": {
                                "seed": guest.seed(),
                                "targets": {
                                    database: 3 if database == name else 1
                                    for database in guest.DATABASES
                                },
                                "files": records,
                            },
                        }
                        try:
                            result = guest.prepare(request, time.monotonic() + 60)
                        except ValueError as exc:
                            if case == "valid-chain":
                                raise
                            if (
                                case == "wrong-version"
                                and str(exc) != "patch target version differs"
                            ):
                                raise AssertionError("version guard was not exercised") from exc
                            if Path("prepared.json").exists():
                                raise AssertionError(
                                    "failed preparation published a database"
                                ) from exc
                        else:
                            if case != "valid-chain":
                                raise AssertionError(f"{name}: {case} was accepted")
                            if result[name]["version"] != 3:
                                raise AssertionError("signed chain did not reach its target")
                            Path("input.bin").write_bytes(b"harmless upload")
                            before = len(scans)
                            verdict, _ = guest.scan(
                                {
                                    "max_bytes": 1024,
                                    "max_filesize": 1024,
                                    "max_scansize": 2048,
                                    "max_files": 10,
                                    "max_recursion": 3,
                                    "timeout": 30,
                                },
                                time.monotonic() + 30,
                            )
                            if verdict != "clean":
                                raise AssertionError("authenticated fixture scan was not clean")
                            if len(scans) != before + 1:
                                raise AssertionError("fixture scan did not reach the native engine")
                            command, code, _ = scans[-1]
                            if code != 0:
                                raise AssertionError(
                                    "authenticated fixture database could not load"
                                )
                            if ("--bytecode-unsigned=yes" in command) != (name == "bytecode"):
                                raise AssertionError("bytecode directory loading policy differs")
                    reports.append(f"{name}:{case}")
                finally:
                    os.chdir(previous)
    return reports


def main() -> None:
    """Load the exact staged guest module without importing host SDK dependencies."""
    spec = importlib.util.spec_from_file_location("antivirus_guest", sys.argv[1])
    if spec is None or spec.loader is None:
        raise ValueError("guest module is unavailable")
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    print(json.dumps(check(guest, Path(sys.argv[2]).resolve())))


if __name__ == "__main__":
    main()
