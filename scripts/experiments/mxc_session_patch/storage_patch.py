"""Manage the separately removable storage overlay above the pinned output overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

if __package__:
    from .output_patch import ROOT, configure, git, state
else:
    from output_patch import ROOT, configure, git, state


def configure_storage(sources: dict[str, Path], build_dir: Path) -> None:
    """Use one pinned Hyperlight source for both host and common crate identities."""
    configure(sources, build_dir)
    manifest = build_dir / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8").replace(
        'default = ["bounded-output"]',
        'default = ["bounded-output", "bounded-storage"]',
    )
    for crate in ("host", "common"):
        path = sources["host"] / f"src/hyperlight_{crate}"
        text += (
            f"hyperlight-{crate} = {{ path = {json.dumps(path.as_posix(), ensure_ascii=False)} }}\n"
        )
    manifest.write_text(text, encoding="utf-8")
    lock = build_dir / "Cargo.lock"
    blocks = lock.read_text(encoding="utf-8").split("[[package]]")
    for index, block in enumerate(blocks):
        if any(f'\nname = "hyperlight-{crate}"\n' in block for crate in ("host", "common")):
            blocks[index] = "\n".join(
                line
                for line in block.split("\n")
                if not line.startswith(("source =", "checksum ="))
            )
    lock.write_text("[[package]]".join(blocks), encoding="utf-8")


def main() -> int:
    """Validate every input before applying or reversing any member of the overlay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "remove", "configure"))
    for option in ("source", "runtime", "host"):
        parser.add_argument(f"--{option}", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    metadata = json.loads((ROOT / "storage-patch.json").read_text(encoding="utf-8"))
    if (
        hashlib.sha256((ROOT / "output-patch.json").read_bytes()).hexdigest()
        != metadata["prerequisite_sha256"]
    ):
        parser.error("output prerequisite checksum differs")
    sources = {
        "session": args.source.resolve(strict=True),
        "runtime": args.runtime.resolve(strict=True),
        "host": args.host.resolve(strict=True),
    }
    observed = {}
    for label, source in sources.items():
        base = metadata["mxc_base" if label == "session" else f"{label}_base"]
        if git(source, "rev-parse", "HEAD").strip() != base:
            parser.error(f"{label} checkout differs from pinned base")
        patch = ROOT / f"storage-{label}.patch"
        if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["patches"][label]["sha256"]:
            parser.error(f"{label} patch checksum differs")
        observed[label] = state(source, metadata["patches"][label]["files"])
    if len(set(observed.values())) != 1:
        parser.error("storage overlays are in different states")
    current = observed["host"]
    if args.action == "check":
        print("applied" if current == "after" else "unapplied")
    elif args.action == "configure":
        if current != "after" or args.build_dir is None:
            parser.error("configure requires applied overlays and a new --build-dir")
        configure_storage(sources, args.build_dir)
    else:
        if current != ("before" if args.action == "apply" else "after"):
            parser.error("overlay already in requested state")
        flags = [] if args.action == "apply" else ["--reverse"]
        for label, source in sources.items():
            git(source, "apply", *flags, "--check", str(ROOT / f"storage-{label}.patch"))
        completed = []
        try:
            for label, source in sources.items():
                git(source, "apply", *flags, str(ROOT / f"storage-{label}.patch"))
                completed.append(label)
        except subprocess.CalledProcessError:
            for label in reversed(completed):
                reverse = ["--reverse"] if not flags else []
                git(sources[label], "apply", *reverse, str(ROOT / f"storage-{label}.patch"))
            raise
        print("applied" if not flags else "removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
