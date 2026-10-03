"""Download and verify the fixed public agent rootfs used by the MXC experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

LAYER_SHA256 = "b7e71eeaaea60deb87456f62bb793ea1503329c130c9b7b8dc9693b998851c4b"
INITRD_SHA256 = "8a9b9e383510dea8bb58b3aca72ac4a8d141f163f6a2fdc68af54197c5922e7e"
MAX_BYTES = 1024**3


def digest(path: Path) -> str:
    """Hash a large runtime file without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract(archive: Path, destination: Path, layer_hash: str, initrd_hash: str) -> None:
    """Extract only the pinned regular initrd, never paths supplied by the archive."""
    if digest(archive) != layer_hash:
        raise ValueError("rootfs layer checksum differs")
    with tarfile.open(archive) as tar:
        matches = [m for m in tar if m.name.removeprefix("./") == "initrd.cpio"]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > MAX_BYTES:
            raise ValueError("rootfs layer must contain one bounded regular initrd")
        source = tar.extractfile(matches[0])
        assert source is not None
        with source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    if digest(destination) != initrd_hash:
        raise ValueError("initrd checksum differs")


def main() -> int:
    """Use a new operator-selected agent directory and an anonymous read-only registry token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = args.destination.resolve()
    root.mkdir(parents=True, exist_ok=False)
    token_url = (
        "https://ghcr.io/token?service=ghcr.io&"
        "scope=repository:hyperlight-dev/hyperlight-unikraft/agent:pull"
    )
    with urllib.request.urlopen(token_url, timeout=60) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        "https://ghcr.io/v2/hyperlight-dev/hyperlight-unikraft/agent/blobs/sha256:" + LAYER_SHA256,
        headers={"Authorization": f"Bearer {token}"},
    )
    archive = root / "layer.tar.gz"
    total = 0
    with urllib.request.urlopen(request, timeout=120) as response, archive.open("xb") as output:
        while data := response.read(1024 * 1024):
            total += len(data)
            if total > MAX_BYTES:
                raise ValueError("rootfs layer exceeds limit")
            output.write(data)
    extract(archive, root / "initrd.cpio", LAYER_SHA256, INITRD_SHA256)
    print(json.dumps({"layer_sha256": LAYER_SHA256, "initrd_sha256": INITRD_SHA256}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
