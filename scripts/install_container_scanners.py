"""Install the release workflow's pinned Linux/amd64 scanners after verifying their bytes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

SCANNERS = {
    "syft": ("1.54.0", "54a87372498168b2d033e876fd41fa4e8035b872699e525a57046e1f2f09c860"),
    "grype": ("0.120.0", "a5a1218dce63acdac152a6b3b5bb366e7267e36f4069848cf455543b3fa5700e"),
}
LIMIT = 128 * 1024 * 1024


def download(name: str, version: str) -> bytes:
    """Use authenticated release assets in Actions; retain public downloads for local use."""
    filename = f"{name}_{version}_linux_amd64.tar.gz"
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        url = f"https://github.com/anchore/{name}/releases/download/v{version}/{filename}"
        # The URL has a fixed HTTPS origin; extraction separately verifies the pinned checksum.
        with urllib.request.urlopen(url, timeout=120) as response:  # nosec B310
            return response.read(LIMIT + 1)
    command = ["gh", "api", "--hostname", "github.com"]
    endpoint = f"repos/anchore/{name}/releases"
    release = json.loads(
        subprocess.check_output([*command, f"{endpoint}/tags/v{version}"], timeout=120)
    )
    assets = [asset for asset in release["assets"] if asset.get("name") == filename]
    if (
        release.get("tag_name") != f"v{version}"
        or len(assets) != 1
        or type(assets[0].get("id")) is not int
        or assets[0]["id"] <= 0
        or type(assets[0].get("size")) is not int
        or not 0 < assets[0]["size"] <= LIMIT
    ):
        raise ValueError("Scanner release asset metadata is invalid")
    asset = assets[0]
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            [
                *command,
                f"{endpoint}/assets/{asset['id']}",
                "-H",
                "Accept: application/octet-stream",
            ],
            stdout=output,
            check=True,
            timeout=120,
        )
        output.seek(0)
        raw = output.read(LIMIT + 1)
    if len(raw) != asset["size"]:
        raise ValueError("Scanner release asset size differs from its metadata")
    return raw


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
        binary = executable(download(name, version), name, expected)
        target = directory / name
        with target.open("xb") as output:
            output.write(binary)
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
