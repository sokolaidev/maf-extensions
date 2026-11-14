"""Verify stable Chrome downloads through Google's signed Debian repository metadata."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import urllib.request
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from container_release import now, timestamp
from vendor_fix_evidence import NoRedirect

BASE = "https://dl.google.com/linux/chrome/deb/"
KEY_URL = "https://dl.google.com/linux/linux_signing_key.pub"
FINGERPRINT = "EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796"
INDEX = "main/binary-amd64/Packages"
PACKAGE = "google-chrome-stable"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-([1-9][0-9]*)"
FILENAME = rf"pool/main/g/{PACKAGE}/{PACKAGE}_{VERSION}_amd64\.deb"
MAX_PACKAGE = 512 * 1024**2


def version(value: str) -> tuple[int, ...]:
    """Order only stable vendor Debian versions, including the package revision."""
    match = re.fullmatch(VERSION, value)
    if match is None:
        raise ValueError("Unsupported Chrome package version")
    return tuple(map(int, match.groups()))


def download(url: str, target: Path, limit: int) -> None:
    """Retain bounded HTTPS bytes without redirects or caller-selected origins."""
    allowed = {
        KEY_URL,
        *(BASE + "dists/stable/" + name for name in ("Release", "Release.gpg", INDEX)),
    }
    if url not in allowed and re.fullmatch(re.escape(BASE) + FILENAME, url) is None:
        raise ValueError("Unapproved Chrome download URL")
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=60) as incoming:
        with target.open("xb") as outgoing:
            count = 0
            while chunk := incoming.read(1024 * 1024):
                count += len(chunk)
                if count > limit:
                    raise ValueError("Chrome download exceeds the size limit")
                outgoing.write(chunk)


def fields(raw: bytes) -> list[dict[str, str]]:
    """Parse Debian control records without accepting duplicate fields."""
    records = []
    for paragraph in raw.decode("utf-8").strip().split("\n\n"):
        values: dict[str, str] = {}
        key = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")) and key is not None:
                values[key] += "\n" + line.strip()
                continue
            name, separator, value = line.partition(":")
            if not separator or not name or name in values:
                raise ValueError("Malformed or duplicate Debian metadata field")
            key = name
            values[key] = value.strip()
        records.append(values)
    return records


def verify_signature(directory: Path) -> None:
    """Trust only Google's published primary fingerprint and its signed subkeys."""
    with tempfile.TemporaryDirectory(prefix="chrome-keyring-") as temporary:
        home = Path(temporary)
        command = ["gpg", "--no-options", "--batch", "--no-autostart", "--homedir", "."]
        subprocess.run(
            [*command, "--import"],
            input=(directory / "google.asc").read_bytes(),
            cwd=home,
            check=True,
            capture_output=True,
            timeout=30,
        )
        key = subprocess.check_output([*command, "--export", FINGERPRINT], cwd=home, timeout=30)
        if not key:
            raise ValueError("Google's reviewed signing key is missing")
        keyring = home / "trusted.gpg"
        keyring.write_bytes(key)
        for name in ("Release", "Release.gpg"):
            (home / name).write_bytes((directory / name).read_bytes())
        result = subprocess.run(
            [
                "gpgv",
                "--homedir",
                ".",
                "--keyring",
                "./trusted.gpg",
                "--status-fd",
                "1",
                "Release.gpg",
                "Release",
            ],
            cwd=home,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        valid = [
            line.split()
            for line in result.stdout.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        if not valid or any(row[-1] != FINGERPRINT for row in valid):
            raise ValueError("Chrome repository signature has an unexpected primary key")


def verify_bytes(path: Path, size: int, sha256: str) -> None:
    """Bind downloaded bytes to the authenticated parent's size and SHA-256."""
    if path.is_symlink() or path.stat().st_size != size:
        raise ValueError("Chrome download size differs from signed metadata")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != sha256:
        raise ValueError("Chrome download checksum differs from signed metadata")


def metadata(directory: Path) -> dict[str, str | int]:
    """Verify the signature chain before selecting the unique stable amd64 package."""
    for name in ("google.asc", "Release", "Release.gpg", "Packages"):
        path = directory / name
        if path.is_symlink() or not 0 < path.stat().st_size <= 1024**2:
            raise ValueError("Invalid Chrome repository evidence file")
    verify_signature(directory)
    releases = fields((directory / "Release").read_bytes())
    if len(releases) != 1:
        raise ValueError("Ambiguous Chrome release metadata")
    release = releases[0]
    if (release.get("Origin"), release.get("Suite"), release.get("Codename")) != (
        "Google LLC",
        "stable",
        "stable",
    ):
        raise ValueError("Unexpected Chrome repository identity")
    date = parsedate_to_datetime(release["Date"])
    if date.tzinfo is None or not -timedelta(minutes=5) <= timestamp(now()) - date < timedelta(
        days=7
    ):
        raise ValueError("Chrome repository metadata is expired or future dated")
    hashes = [line.split() for line in release["SHA256"].splitlines() if line.strip()]
    entries = [row for row in hashes if len(row) == 3 and row[2] == INDEX]
    if len(entries) != 1 or re.fullmatch(r"[0-9a-f]{64}", entries[0][0]) is None:
        raise ValueError("Missing or ambiguous Chrome package index hash")
    checksum, size, _ = entries[0]
    verify_bytes(directory / "Packages", int(size), checksum)
    packages = [
        row
        for row in fields((directory / "Packages").read_bytes())
        if row.get("Package") == PACKAGE
    ]
    if len(packages) != 1:
        raise ValueError("Missing or ambiguous stable Chrome package")
    package = packages[0]
    selected = package["Version"]
    version(selected)
    filename = f"pool/main/g/{PACKAGE}/{PACKAGE}_{selected}_amd64.deb"
    if package.get("Architecture") != "amd64" or package.get("Filename") != filename:
        raise ValueError("Unexpected stable Chrome architecture or filename")
    if (
        re.fullmatch(r"[0-9a-f]{64}", package["SHA256"]) is None
        or not 0 < int(package["Size"]) <= MAX_PACKAGE
    ):
        raise ValueError("Invalid Chrome package size or SHA-256")
    return {
        "version": selected,
        "url": BASE + filename,
        "sha256": package["SHA256"],
        "size": int(package["Size"]),
    }


def acquire(directory: Path) -> dict[str, str | int]:
    """Retain metadata and the verified archive for qualification and review."""
    directory.mkdir(parents=True, exist_ok=False)
    for name, url in {
        "google.asc": KEY_URL,
        "Release": BASE + "dists/stable/Release",
        "Release.gpg": BASE + "dists/stable/Release.gpg",
        "Packages": BASE + "dists/stable/" + INDEX,
    }.items():
        download(url, directory / name, 1024**2)
    selected = metadata(directory)
    download(str(selected["url"]), directory / "chrome.deb", int(selected["size"]))
    verify_bytes(directory / "chrome.deb", int(selected["size"]), str(selected["sha256"]))
    return selected
