"""Apply exact MXC 1.0 experiment layers and configure an isolated probe build."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from scripts.experiments.mxc_session_patch.output_patch import fingerprint, git, state

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT.with_name("mxc_session_patch")


def metadata() -> dict:
    """Return the release and affected-file pins for this experiment."""
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def overlay(action: str, layer: str, sources: dict[str, Path]) -> None:
    """Validate every affected checkout before applying or reversing a layer."""
    pins = metadata()
    layers = ("session", "output", "storage")
    expected = {label: {} for label in sources}
    for label, source in sources.items():
        pin = pins["mxc_base" if label == "session" else f"{label}_base"]
        if git(source, "rev-parse", "HEAD").strip() != pin:
            raise ValueError(f"{label} checkout differs from pinned base")
    for name in layers:
        for label, entry in pins["layers"][name].items():
            patch = ROOT / f"{name}-{label}.patch"
            if hashlib.sha256(patch.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"{name}/{label} patch checksum differs")
            for path, hashes in entry["files"].items():
                expected[label].setdefault(path, hashes["before"])
    phases = []
    for phase in range(4):
        if all(
            fingerprint(sources[label] / path) == value
            for label, files in expected.items()
            for path, value in files.items()
        ):
            phases.append(phase)
        if phase < 3:
            for label, entry in pins["layers"][layers[phase]].items():
                expected[label].update(
                    {path: hashes["after"] for path, hashes in entry["files"].items()}
                )
    if len(phases) != 1:
        raise ValueError("layer files or prerequisites are modified or mixed")
    index = layers.index(layer)
    if action == "check":
        if phases[0] not in (index, index + 1):
            raise ValueError("check requires the layer or its immediate prerequisite")
        print("after" if phases[0] == index + 1 else "before")
        return
    if phases[0] != index + (action == "remove"):
        raise ValueError("layer order or requested state is invalid")
    entries = pins["layers"][layer]
    flags = [] if action == "apply" else ["--reverse"]
    for label in entries:
        git(sources[label], "apply", *flags, "--check", str(ROOT / f"{layer}-{label}.patch"))
    completed = []
    try:
        for label in entries:
            git(sources[label], "apply", *flags, str(ROOT / f"{layer}-{label}.patch"))
            completed.append(label)
    except subprocess.CalledProcessError:
        for label in reversed(completed):
            git(
                sources[label],
                "apply",
                *([] if flags else ["--reverse"]),
                str(ROOT / f"{layer}-{label}.patch"),
            )
        raise


def configure(sources: dict[str, Path], build: Path) -> None:
    """Reuse the qualified probe, changing only backend imports and reported MXC identity."""
    overlay("check", "storage", sources)
    pins = metadata()
    for label, entry in pins["layers"]["storage"].items():
        if state(sources[label], entry["files"]) != "after":
            raise ValueError("configure requires the storage layer")
    build.mkdir(parents=True, exist_ok=False)
    for name in ("main", "backend", "owner"):
        text = (BASELINE / f"probe/{name}.rs").read_text(encoding="utf-8")
        if name == "backend":
            if text.count("use hyperlight_common::") != 2 or text.count("use wxc_common::") != 1:
                raise ValueError("probe imports changed; inspect the compatibility wrapper")
            text = text.replace(
                "use hyperlight_common::", "use mxc_sdk::hyperlight_common::"
            ).replace("use wxc_common::", "use mxc_sdk::mxc_common::")
        if name == "main":
            old = json.loads((BASELINE / "patch.json").read_text(encoding="utf-8"))["base"]
            if text.count(old) != 2:
                raise ValueError("probe report identity changed")
            text = text.replace(old, pins["mxc_base"])
        (build / f"{name}.rs").write_text(text, encoding="utf-8")
    paths = {
        "@SDK@": sources["session"] / "src/mxc-sdk",
        "@RUNTIME@": sources["runtime"],
        "@HOST@": sources["host"] / "src/hyperlight_host",
        "@COMMON@": sources["host"] / "src/hyperlight_common",
    }
    manifest = (ROOT / "Cargo.toml.template").read_text(encoding="utf-8")
    for key, path in paths.items():
        manifest = manifest.replace(key, json.dumps(path.as_posix()))
    (build / "Cargo.toml").write_text(manifest, encoding="utf-8")
    shutil.copyfile(ROOT / "Cargo.lock", build / "Cargo.lock")


def main() -> int:
    """Manage operator-selected, pinned upstream checkouts without touching registry caches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "remove", "check", "configure"))
    parser.add_argument("--layer", choices=("session", "output", "storage"), default="storage")
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
        overlay(args.action, args.layer, sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
