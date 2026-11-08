"""Build one provider with the verified tooling snapshot supplied by preparation."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from container_release import ROOT, read, write  # noqa: E402
from plan_go_remediation import CONTEXT  # noqa: E402
from prepare_go_remediation import resolve_modules  # noqa: E402


def build(provider: dict[str, Any], work: Path) -> tuple[bytes, dict[str, str]]:
    """Use the authenticated source builder and its pinned local Go toolchain."""
    spec = importlib.util.spec_from_file_location(
        "remediation_provider_builder", ROOT / CONTEXT / "build_providers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(provider, work, resolve_modules=resolve_modules)


def main() -> None:
    """Return package bytes, regenerated module hashes and resolved pins to preparation."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("request", "work", "result", "package"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    provider = read(args.request)
    data, hashes = build(provider, args.work)
    args.package.write_bytes(data)
    write(args.result, {"moduleFiles": hashes, "modules": provider["source_build"]["modules"]})


if __name__ == "__main__":
    main()
