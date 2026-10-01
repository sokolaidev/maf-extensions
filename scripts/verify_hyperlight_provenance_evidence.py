"""Verify original signature and manifest bytes offline against an operator-approved receipt."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from hyperlight_evidence import extract_archive, read, verify_bundle


def verify(archive: Path, receipt: Path) -> None:
    """Require independently approved archive, trust and publisher policy."""
    expected = read(receipt)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        extract_archive(archive, root, expected.get("archive_sha256"))
        verify_bundle(root, expected["candidate"], expected["trusted_root_sha256"])


def main() -> None:
    """Check signed provenance; unsigned packaging and acceptance reports remain separate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.receipt)
    print(json.dumps({"signed_provenance_verified": True}))


if __name__ == "__main__":
    main()
