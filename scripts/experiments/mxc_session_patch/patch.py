"""Apply or remove the pinned experimental MXC session patch without merging drift."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def git(source: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run Git with literal arguments in the explicitly selected checkout."""
    return subprocess.run(
        ["git", "-C", str(source), *args], text=True, capture_output=True, check=check
    )


def main() -> int:
    """Refuse another MXC base or a patch that cannot be applied/reversed exactly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "remove", "configure"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    metadata = json.loads((ROOT / "patch.json").read_text(encoding="utf-8"))
    patch = ROOT / "session-preview.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["sha256"]:
        parser.error("patch checksum differs from patch.json")
    if git(source, "rev-parse", "HEAD").stdout.strip() != metadata["base"]:
        parser.error("MXC checkout does not match the pinned base; no fuzzy application is allowed")
    forward = git(source, "apply", "--check", str(patch), check=False).returncode == 0
    reverse = git(source, "apply", "--reverse", "--check", str(patch), check=False).returncode == 0
    if forward == reverse:
        parser.error("patch state is ambiguous or modified; inspect the checkout manually")
    if args.action == "check":
        print("applied" if reverse else "unapplied")
    elif args.action == "apply":
        if not forward or git(source, "status", "--porcelain").stdout.strip():
            parser.error("apply requires a clean checkout with the patch absent")
        git(source, "apply", str(patch))
        print("applied")
    elif args.action == "remove":
        if not reverse:
            parser.error("patch is absent or has been edited; refusing removal")
        git(source, "apply", "--reverse", str(patch))
        print("removed")
    else:
        if not reverse or args.build_dir is None:
            parser.error("configure requires the applied patch and --build-dir")
        build = args.build_dir.resolve()
        build.mkdir(parents=True, exist_ok=False)
        template = (ROOT / "Cargo.toml.template").read_text(encoding="utf-8")
        manifest = template
        for key, path in {
            "@MAIN@": ROOT / "probe/main.rs",
            "@COMMON@": source / "src/backends/hyperlight/common",
            "@WXC@": source / "src/core/wxc_common",
        }.items():
            manifest = manifest.replace(key, json.dumps(path.as_posix()))
        (build / "Cargo.toml").write_text(manifest, encoding="utf-8")
        shutil.copyfile(ROOT / "Cargo.lock", build / "Cargo.lock")
        print(
            f"configured {build}; build with cargo build --locked --manifest-path <build-dir>/Cargo.toml"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
