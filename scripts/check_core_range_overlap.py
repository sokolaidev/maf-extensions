"""Check built dependents' core ranges without an index or a local-core override.

This checks version constraints for the current environment, without extras. The
whole-set install remains responsible for artifacts and other dependency conflicts.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from check_published_dependents_admit import INDEPENDENT_SANDBOX_PACKAGES
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent


def declared_core_ranges(dist_dir: Path) -> dict[str, SpecifierSet]:
    """Read active core requirements from sandbox family wheels."""
    wheels = sorted(dist_dir.glob("*.whl"))
    if not wheels:
        raise ValueError(f"no wheels in {dist_dir} — build them first")
    ranges: dict[str, SpecifierSet] = {}
    seen: set[str] = set()
    for wheel in wheels:
        name, _, _, _ = parse_wheel_filename(wheel.name)
        if name in INDEPENDENT_SANDBOX_PACKAGES:
            continue
        if name in seen:
            raise ValueError(f"more than one wheel for {name}")
        seen.add(name)
        if name == _CORE:
            continue
        with zipfile.ZipFile(wheel) as archive:
            entries = [
                entry for entry in archive.namelist() if entry.endswith(".dist-info/METADATA")
            ]
            if len(entries) != 1:
                raise ValueError(f"{wheel.name} must carry exactly one METADATA entry")
            metadata = Parser().parsestr(archive.read(entries[0]).decode("utf-8"))
        for entry in metadata.get_all("Requires-Dist", []):
            requirement = Requirement(entry)
            if canonicalize_name(requirement.name) != _CORE:
                continue
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            if requirement.url is not None:
                raise ValueError(f"{name}: a direct core URL has no declared version range")
            # Publication and prerelease selection belong to the install check.
            ranges.setdefault(name, SpecifierSet(prereleases=True))
            ranges[name] &= str(requirement.specifier)
    if not ranges:
        raise ValueError("no wheel declares an active maf-sandbox requirement")
    return ranges


def intersection(ranges: dict[str, SpecifierSet]) -> SpecifierSet:
    """Combine all declared constraints, including exclusions and exact pins."""
    combined = SpecifierSet(prereleases=True)
    for specifiers in ranges.values():
        combined &= str(specifiers)
    return combined


def main(argv: list[str] | None = None) -> int:
    """Refuse a set whose dependents cannot share any core version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=_ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        ranges = declared_core_ranges(args.dist_dir)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    if intersection(ranges).is_unsatisfiable():
        print(
            "::error::No maf-sandbox version satisfies all dependent core ranges.", file=sys.stderr
        )
        for name, specifiers in sorted(ranges.items()):
            print(f"  {name}: {_CORE}{specifiers}", file=sys.stderr)
        print(
            "Move the dependent ranges together before applying a local-core override.",
            file=sys.stderr,
        )
        return 1
    print(f"OK  {len(ranges)} dependents have overlapping maf-sandbox ranges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
