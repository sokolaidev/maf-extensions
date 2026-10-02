"""Check fixed guest state across an abrupt native-helper process restart."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOTFS_SHA256 = "8a9b9e383510dea8bb58b3aca72ac4a8d141f163f6a2fdc68af54197c5922e7e"


def digest(path: Path) -> str:
    """Hash an artifact without reading it all into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    """Require a new output directory so stale reports cannot satisfy this probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--initrd", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--image-home", type=Path, help="Prepared MXC image home for runner controls"
    )
    args = parser.parse_args()
    helper = args.helper.resolve(strict=True)
    initrd = args.initrd.resolve(strict=True)
    if digest(initrd) != ROOTFS_SHA256:
        parser.error("initrd does not match the recorded MXC agent image")
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    checkpoint = state / "checkpoint"
    seed_report = state / "seed.json"
    restore_report = state / "restore.json"
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update(
        {
            key: str(state)
            for key in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TMP", "TEMP", "TMPDIR")
        }
    )
    if args.image_home is not None:
        env["MXC_HYPERLIGHT_HOME"] = str(args.image_home.resolve(strict=True))
    with (state / "seed.stdout").open("wb") as stdout, (state / "seed.stderr").open("wb") as stderr:
        child = subprocess.Popen(
            [str(helper), "seed", str(initrd), str(checkpoint), str(seed_report), "hold"],
            cwd=state,
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 180
            while not seed_report.exists():
                if child.poll() is not None:
                    raise RuntimeError(f"seed exited before saving a report: {child.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("seed did not finish in 180 seconds")
                time.sleep(0.1)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
    seed = json.loads(seed_report.read_text(encoding="utf-8"))
    if seed.get("snapshot_saved") is not True or seed.get("post_snapshot_mutation") is not True:
        raise RuntimeError("seed report is incomplete")
    artifacts = {
        path.relative_to(checkpoint).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in sorted(checkpoint.rglob("*"))
        if path.is_file()
    }
    with (
        (state / "restore.stdout").open("wb") as stdout,
        (state / "restore.stderr").open("wb") as stderr,
    ):
        restored = subprocess.run(
            [str(helper), "restore", str(checkpoint), str(restore_report)],
            cwd=state,
            env=env,
            stdout=stdout,
            stderr=stderr,
            timeout=120,
            check=False,
        )
    if restored.returncode != 0:
        raise RuntimeError(f"restore failed: {restored.returncode}")
    recovered = json.loads(restore_report.read_text(encoding="utf-8"))
    if recovered.get("restored_state_verified") is not True or recovered.get("pid") == seed.get(
        "pid"
    ):
        raise RuntimeError("restore report does not establish a new process with matching state")
    if any(digest(checkpoint / path) != entry["sha256"] for path, entry in artifacts.items()):
        raise RuntimeError("restoring changed the saved checkpoint")
    refusals: dict[str, int] = {}
    index = json.loads((checkpoint / "index.json").read_text(encoding="utf-8"))
    mismatched = json.loads(json.dumps(index))
    for manifest in mismatched["manifests"]:
        manifest["annotations"]["org.opencontainers.image.ref.name"] = (
            "0.14.1-k0000000000000000-c999"
        )
    for name, content in {
        "truncated_index": "{",
        "missing_blobs": json.dumps(index),
        "incompatible_key": json.dumps(mismatched),
    }.items():
        invalid = state / name
        invalid.mkdir()
        (invalid / "index.json").write_text(content, encoding="utf-8")
        (invalid / "oci-layout").write_bytes((checkpoint / "oci-layout").read_bytes())
        report = state / f"{name}.json"
        with (
            (state / f"{name}.stdout").open("wb") as stdout,
            (state / f"{name}.stderr").open("wb") as stderr,
        ):
            refused = subprocess.run(
                [str(helper), "restore", str(invalid), str(report)],
                cwd=state,
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=30,
                check=False,
            )
        if refused.returncode != 1 or report.exists():
            raise RuntimeError(f"{name} was not explicitly refused")
        refusals[name] = refused.returncode
    result = {
        "refusals": refusals,
        "helper_sha256": digest(helper),
        "initrd_sha256": ROOTFS_SHA256,
        "seed": seed,
        "restore": recovered,
        "seed_killed_after_save": True,
        "snapshot_unchanged_after_restore": True,
        "snapshot_artifacts": artifacts,
    }
    (state / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS: native Python state survived a killed helper and new-process restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
