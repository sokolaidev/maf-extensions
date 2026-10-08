"""Run the original native-state controls against the release-pinned MXC 1.0 rootfs."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.experiments import mxc_native_state_probe


def main() -> int:
    """Select only the image identity; preserve every original native assertion."""
    pins = json.loads(Path(__file__).with_name("rootfs.json").read_text(encoding="utf-8"))
    mxc_native_state_probe.ROOTFS_SHA256 = pins["initrd_sha256"]
    return mxc_native_state_probe.main()


if __name__ == "__main__":
    raise SystemExit(main())
