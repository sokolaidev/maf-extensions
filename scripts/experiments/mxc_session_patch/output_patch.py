"""Manage the separately pinned native-output overlay on the MXC session experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def git(source: Path, *args: str) -> str:
    """Run Git against an explicitly selected source checkout."""
    return subprocess.check_output(["git", "-C", str(source), *args], text=True)


def fingerprint(path: Path) -> str | None:
    """Hash normalized patch text, treating an absent new file separately."""
    if not path.exists():
        return None
    # Git patches describe normalized text, independent of checkout line endings.
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()


def state(source: Path, files: dict[str, dict[str, str | None]]) -> str:
    """Refuse drift before applying or reversing either overlay."""
    matches = [
        side
        for side in ("before", "after")
        if all(fingerprint(source / name) == values[side] for name, values in files.items())
    ]
    if len(matches) != 1:
        raise ValueError("overlay files are modified or mixed; refusing to overwrite them")
    return matches[0]


def configure(sources: dict[str, Path], build_dir: Path) -> None:
    """Write a new isolated build using the pinned output overlay and lockfile."""
    build_dir.mkdir(parents=True, exist_ok=False)
    template = (ROOT / "Cargo.toml.template").read_text(encoding="utf-8")
    for key, path in {
        "@MAIN@": ROOT / "probe/main.rs",
        "@COMMON@": sources["session"] / "src/backends/hyperlight/common",
        "@WXC@": sources["session"] / "src/core/wxc_common",
    }.items():
        template = template.replace(key, json.dumps(path.as_posix()))
    (build_dir / "Cargo.toml").write_text(template, encoding="utf-8")
    shutil.copyfile(ROOT / "Cargo.lock", build_dir / "Cargo.lock")
    manifest = build_dir / "Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[features]\nbounded-output = []\nbounded-storage = []\n", ""
        )
        + '\n[features]\ndefault = ["bounded-output"]\nbounded-storage = []\nbounded-output = ["hyperlight_common/maf-output-preview"]\n'
        "\n[patch.crates-io]\nhyperlight-unikraft = { path = "
        + json.dumps(sources["runtime"].as_posix())
        + " }\n",
        encoding="utf-8",
    )
    lock = build_dir / "Cargo.lock"
    blocks = lock.read_text(encoding="utf-8").split("[[package]]")
    for index, block in enumerate(blocks):
        if '\nname = "hyperlight-unikraft"\n' in block:
            blocks[index] = "\n".join(
                line
                for line in block.split("\n")
                if not line.startswith(("source =", "checksum ="))
            )
    lock.write_text("[[package]]".join(blocks), encoding="utf-8")


def main() -> int:
    """Validate both overlays before mutation and configure an isolated locked build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "remove", "configure"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    metadata = json.loads((ROOT / "output-patch.json").read_text(encoding="utf-8"))
    prerequisite = hashlib.sha256((ROOT / "session-preview.patch").read_bytes()).hexdigest()
    if prerequisite != metadata["prerequisite_sha256"]:
        parser.error("session prerequisite checksum differs")
    sources = {
        "session": args.source.resolve(strict=True),
        "runtime": args.runtime.resolve(strict=True),
    }
    observed = {}
    for label, source in sources.items():
        base = metadata["mxc_base" if label == "session" else "runtime_base"]
        if git(source, "rev-parse", "HEAD").strip() != base:
            parser.error(f"{label} checkout differs from pinned base")
        patch = ROOT / f"output-{label}.patch"
        if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["patches"][label]["sha256"]:
            parser.error(f"{label} patch checksum differs")
        observed[label] = state(source, metadata["patches"][label]["files"])
    if len(set(observed.values())) != 1:
        parser.error("runtime and session overlays are in different states")
    current = observed["runtime"]
    if args.action == "check":
        print("applied" if current == "after" else "unapplied")
    elif args.action in ("apply", "remove"):
        expected = "before" if args.action == "apply" else "after"
        if current != expected:
            parser.error("overlay already in requested state")
        flags = [] if args.action == "apply" else ["--reverse"]
        for label, source in sources.items():
            git(source, "apply", *flags, "--check", str(ROOT / f"output-{label}.patch"))
        completed = []
        try:
            for label, source in sources.items():
                git(source, "apply", *flags, str(ROOT / f"output-{label}.patch"))
                completed.append(label)
        except subprocess.CalledProcessError:
            for label in reversed(completed):
                reverse = ["--reverse"] if not flags else []
                git(sources[label], "apply", *reverse, str(ROOT / f"output-{label}.patch"))
            raise
        print("applied" if not flags else "removed")
    else:
        if current != "after" or args.build_dir is None:
            parser.error("configure requires applied overlays and a new --build-dir")
        configure(sources, args.build_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
