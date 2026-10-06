"""Export the validated immutable catalogue for an Actions-deployed public status page."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from container_release import ROOT, empty_catalogue
from container_release_history import History, encode, sha256


def export(directory: Path, history: History | None = None) -> None:
    """Publish neutral presentation assets; the browser checks current GitHub state on every view."""
    snapshot = (history or History()).head()
    raw = encode(snapshot.document if snapshot else {"sequence": 0, "catalogue": empty_catalogue()})
    if snapshot and sha256(raw) != snapshot.digest:
        raise ValueError("Exported catalogue bytes differ from the immutable snapshot")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "catalogue.json").write_bytes(raw)
    for name in ("index.html", "status.js", "badge.svg"):
        shutil.copyfile(ROOT / "docs/security/status" / name, directory / name)


def main() -> None:
    """Create the Pages artifact without changing branches or repository settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    export(parser.parse_args().directory)


if __name__ == "__main__":
    main()
