"""Install the release workflow's pinned Linux/amd64 scanners after verifying their bytes."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

SCANNERS = {
    "syft": ("1.54.0", "54a87372498168b2d033e876fd41fa4e8035b872699e525a57046e1f2f09c860"),
    "grype": ("0.120.0", "a5a1218dce63acdac152a6b3b5bb366e7267e36f4069848cf455543b3fa5700e"),
}
LIMIT = 128 * 1024 * 1024


def executable(raw: bytes, name: str, expected: str) -> bytes:
    """Read only the named regular executable from an authenticated archive."""
    if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Scanner archive differs from its pinned checksum")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        member = archive.getmember(name)
        if not member.isfile() or not 0 < member.size <= LIMIT:
            raise ValueError("Scanner executable is not a bounded regular file")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("Scanner executable is missing")
        return source.read(LIMIT + 1)


def install(directory: Path) -> None:
    """Install without executing downloaded installers or altering the system package manager."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("Release scanners require Linux/amd64")
    directory.mkdir(parents=True, exist_ok=True)
    for name, (version, expected) in SCANNERS.items():
        url = f"https://github.com/anchore/{name}/releases/download/v{version}/{name}_{version}_linux_amd64.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as response:
            raw = response.read(LIMIT + 1)
        target = directory / name
        with target.open("xb") as output:
            output.write(executable(raw, name, expected))
        target.chmod(0o755)
    with Path(os.environ["GITHUB_PATH"]).open("a", encoding="utf-8") as output:
        output.write(str(directory.resolve()) + "\n")


def main() -> None:
    """Install the checked-in scanner versions into a fresh workflow tool directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    install(parser.parse_args().directory)


if __name__ == "__main__":
    main()
