"""Configure and verify the configuration-ID image store on hosted Linux runners."""

from __future__ import annotations

import json
import sys
from typing import Any


def configure(value: Any) -> dict[str, Any]:
    """Preserve daemon settings while disabling the containerd image store."""
    if not isinstance(value, dict):
        raise ValueError("Docker daemon configuration must be an object")
    features = value.get("features", {})
    if not isinstance(features, dict):
        raise ValueError("Docker daemon features must be an object")
    return {**value, "features": {**features, "containerd-snapshotter": False}}


def verify(value: Any) -> None:
    """Require the effective Linux overlay2 driver, not just a configuration setting."""
    if (
        not isinstance(value, dict)
        or value.get("OSType") != "linux"
        or value.get("Driver") != "overlay2"
        or any("io.containerd.snapshotter" in str(row) for row in value.get("DriverStatus") or [])
    ):
        raise ValueError("Container release jobs require Docker's classic Linux overlay2 store")


def main() -> None:
    """Transform daemon configuration or check Docker info supplied as JSON on stdin."""
    if sys.argv[1:] == ["configure"]:
        json.dump(configure(json.load(sys.stdin)), sys.stdout)
        print()
    elif sys.argv[1:] == ["verify"]:
        verify(json.load(sys.stdin))
        print("Docker classic Linux overlay2 image store verified")
    else:
        raise SystemExit("usage: classic_docker_store.py configure|verify (JSON on stdin)")


if __name__ == "__main__":
    main()
