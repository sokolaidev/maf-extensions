"""Apply the byte-stream layer above the qualified MXC 1.0 experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from scripts.experiments.mxc_session_patch.output_patch import fingerprint, git
from scripts.experiments.mxc_v1_patch import patch as baseline

ROOT = Path(__file__).resolve().parent


def overlay(action: str, sources: dict[str, Path]) -> None:
    """Check all prerequisites before changing either native checkout."""
    pins = baseline.metadata()
    layers = json.loads((ROOT / "streams.json").read_text(encoding="utf-8"))["layers"]
    expected = {label: {} for label in sources}
    for label, source in sources.items():
        if (
            git(source, "rev-parse", "HEAD").strip()
            != pins["mxc_base" if label == "session" else f"{label}_base"]
        ):
            raise ValueError("upstream source pin differs")
    for entries in pins["layers"].values():
        for label, entry in entries.items():
            expected[label].update(
                {path: values["after"] for path, values in entry["files"].items()}
            )
    phases = []
    for side in ("before", "after"):
        wanted = {label: dict(files) for label, files in expected.items()}
        for label, entry in layers.items():
            patch = ROOT / f"streams-{label}.patch"
            if hashlib.sha256(patch.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("stream patch checksum differs")
            wanted[label].update({path: values[side] for path, values in entry["files"].items()})
        if all(
            fingerprint(sources[label] / path) == value
            for label, files in wanted.items()
            for path, value in files.items()
        ):
            phases.append(side)
    if len(phases) != 1:
        raise ValueError("stream layer or prerequisites are modified or mixed")
    if action == "check":
        print(phases[0])
        return
    if phases[0] != ("before" if action == "apply" else "after"):
        raise ValueError("stream layer is already in requested state")
    flags = ["--reverse"] if action == "remove" else []
    for label in layers:
        git(sources[label], "apply", *flags, "--check", str(ROOT / f"streams-{label}.patch"))
    completed = []
    try:
        for label in layers:
            git(sources[label], "apply", *flags, str(ROOT / f"streams-{label}.patch"))
            completed.append(label)
    except subprocess.CalledProcessError:
        for label in reversed(completed):
            git(
                sources[label],
                "apply",
                *([] if flags else ["--reverse"]),
                str(ROOT / f"streams-{label}.patch"),
            )
        raise


def configure(sources: dict[str, Path], build: Path) -> None:
    """Configure the baseline helper before installing the stream-specific wrapper."""
    baseline.configure(sources, build)
    overlay("apply", sources)
    for name in ("streams",):
        shutil.copyfile(ROOT / f"{name}.rs", build / f"{name}.rs")
    path = build / "main.rs"
    text = path.read_text(encoding="utf-8").replace("mod backend;", "mod streams;\nmod backend;", 1)
    text = text.replace(
        "    match args.get(1).map(String::as_str) {",
        "    if streams::run(&args)? { return Ok(()); }\n    match args.get(1).map(String::as_str) {",
        1,
    )
    path.write_text(text, encoding="utf-8")
    path = build / "backend.rs"
    text = path.read_text(encoding="utf-8")
    text += (ROOT / "backend.inc.rs").read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")


def main() -> int:
    """Manage pinned native sources without changing installed runtimes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "remove", "check", "configure"))
    for name in ("source", "runtime", "host"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    sources = {
        "session": args.source.resolve(strict=True),
        "runtime": args.runtime.resolve(strict=True),
        "host": args.host.resolve(strict=True),
    }
    if args.action == "configure":
        if args.build_dir is None:
            parser.error("configure requires --build-dir")
        configure(sources, args.build_dir.resolve())
    else:
        overlay(args.action, sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
