"""Build one pinned Linux helper/startup fixture for all cross-runner participants."""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from pathlib import Path

from scripts.experiments.mxc_v1_patch import qualification_runner as native
from scripts.experiments.mxc_v1_patch.patch import metadata

from . import cross_compat


def build(root: Path) -> None:
    """Reuse the locked overlay build and native startup controls."""
    root.mkdir(parents=True, exist_ok=False)
    producer_vendor = cross_compat.cpu_vendor()
    native.hypervisor()
    opts = native.sources(root, metadata())
    tool = [sys.executable, "-m", "scripts.experiments.mxc_v1_patch.patch"]
    for layer in ("session", "output", "storage"):
        native.run(root, f"apply-{layer}", *tool, "apply", "--layer", layer, *opts)
    native.run(root, "configure", *tool, "configure", *opts, "--build-dir", str(root / "build"))
    native.build(root)
    native.run(
        root,
        "prepare-agent",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.prepare_agent",
        "--destination",
        str(root / "images/agent"),
    )
    (root / "images/agent/VERSION").write_text(
        "rootfs: ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v0.17.0\n",
        encoding="utf-8",
    )
    native.run(
        root,
        "native-state",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.native_probe",
        "--helper",
        str(native.helper(root)),
        "--initrd",
        str(root / "images/agent/initrd.cpio"),
        "--image-home",
        str(root / "images"),
        "--state-dir",
        str(root / "native"),
    )
    bundle = root / "bundle"
    bundle.mkdir()
    shutil.copy2(native.helper(root), bundle / "helper")
    shutil.copytree(root / "images/agent/snapshot", bundle / "startup")
    cross_compat.record_fixture(bundle, producer_vendor)
    with tarfile.open(root / "bundle.tar.gz", "w:gz") as archive:
        archive.add(bundle, arcname="bundle")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    build(parser.parse_args().root)
