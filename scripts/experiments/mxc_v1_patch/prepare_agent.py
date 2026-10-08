"""Prepare the verified agent rootfs for the MXC 1.0 migration experiment."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.experiments.mxc_session_patch import prepare_agent


def main() -> int:
    """Reuse the bounded extractor with release-specific artifact hashes."""
    pins = json.loads(Path(__file__).with_name("rootfs.json").read_text(encoding="utf-8"))
    prepare_agent.LAYER_SHA256 = pins["layer_sha256"]
    prepare_agent.INITRD_SHA256 = pins["initrd_sha256"]
    return prepare_agent.main()


if __name__ == "__main__":
    raise SystemExit(main())
