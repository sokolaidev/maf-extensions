"""Manage the opt-in byte-stream device against exact kernel source pins."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.experiments.mxc_session_patch.output_patch import git, state

ROOT = Path(__file__).resolve().parent


def metadata() -> dict:
    """Return the source and affected-file identities for the kernel layer."""
    return json.loads((ROOT / "kernel.json").read_text(encoding="utf-8"))


def overlay(action: str, source: Path) -> None:
    """Reject source drift before applying or reversing the device patch."""
    pins = metadata()
    if git(source, "rev-parse", "HEAD").strip() != pins["runtime_base"]:
        raise ValueError("runtime source differs from the pinned release")
    for name, commit in pins["submodules"].items():
        if git(source / name, "rev-parse", "HEAD").strip() != commit:
            raise ValueError(f"{name} differs from its pinned source")
    patch = ROOT / "kernel.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != pins["patch_sha256"]:
        raise ValueError("kernel patch checksum differs")
    kernel = source / "kernel/unikraft"
    current = state(kernel, pins["files"])
    if current not in ("before", "after"):
        raise ValueError("kernel prerequisites are modified or mixed")
    if action == "check":
        print(current)
        return
    if current != ("before" if action == "apply" else "after"):
        raise ValueError("kernel patch is already in the requested state")
    flags = ["--reverse"] if action == "remove" else []
    git(kernel, "apply", *flags, "--check", str(patch))
    git(kernel, "apply", *flags, str(patch))


def main() -> int:
    """Apply only to an explicitly selected upstream checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "remove"))
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    overlay(args.action, args.source.resolve(strict=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
