"""Qualify aggregate export bounds across the new memory and transport blobs."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from scripts.experiments.mxc_session_patch.host_store import MAX_CHECKPOINT, MAX_FILES


def measure(root: Path) -> tuple[int, int]:
    """Count physical files, including metadata and transport blobs."""
    files = [path for path in root.rglob("*") if path.is_file()]
    return sum(path.stat().st_size for path in files), len(files)


def qualify(helper: Path, startup: Path, root: Path) -> dict:
    """Require exact export limits to pass and one-byte/file deficits to refuse."""
    root.mkdir(parents=True, exist_ok=False)
    code = root / "code.py"
    code.write_text("pass\n", encoding="utf-8")

    def execute(name: str, bounds: tuple[int, int], success: bool) -> tuple[int, int]:
        checkpoint = root / name
        report = root / f"{name}.json"
        with (root / f"{name}.log").open("wb") as log:
            child = subprocess.run(
                [
                    str(helper),
                    "call-stored",
                    str(startup),
                    str(checkpoint),
                    str(code),
                    str(report),
                    "0",
                    str(bounds[0]),
                    str(bounds[1]),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        actual = measure(checkpoint)
        if actual[0] > bounds[0] or actual[1] > bounds[1]:
            raise RuntimeError(f"{name}: export exceeded reserved bounds")
        if success:
            if child.returncode != 0 or not report.is_file():
                raise RuntimeError(f"{name}: exact export failed")
        elif (
            child.returncode == 0
            or report.exists()
            or "snapshot export exceeds reserved bounds"
            not in (root / f"{name}.log").read_text(encoding="utf-8")
        ):
            raise RuntimeError(f"{name}: missing bounded-export refusal")
        return actual

    budget = execute("measured", (MAX_CHECKPOINT, MAX_FILES), True)
    if budget[0] <= 1 or budget[1] <= 1:
        raise RuntimeError("checkpoint did not contain snapshot data and metadata")
    exact = execute("exact", budget, True)
    if exact != budget:
        raise RuntimeError("same startup and code produced different export sizes")
    deficits = {}
    for name, bounds in (
        ("one-byte-short", (budget[0] - 1, budget[1])),
        ("one-file-short", (budget[0], budget[1] - 1)),
    ):
        deficits[name] = {"bounds": bounds, "written": execute(name, bounds, False)}
    result = {"exact_bytes": budget[0], "exact_files": budget[1], "refusals": deficits}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    """Use only the explicitly selected candidate helper and startup checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.helper.resolve(), args.startup.resolve(), args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
