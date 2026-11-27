"""Native fixture and execution-host admission for the cross-runner experiment."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

from maf_sandbox_mxc import startup_identity
from maf_sandbox_mxc.host_call import digest

VENDORS = ("GenuineIntel", "AuthenticAMD")


def binding() -> dict[str, str]:
    """Bind fixture provenance and discovery to one source and Actions attempt."""
    return {key: os.environ[key] for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}


def helper_path(bundle: Path, system: str) -> Path:
    """Select the executable recorded by the fixture producer."""
    return bundle / ("helper.exe" if system == "Windows" else "helper")


def windows_cpu_vendors() -> list[str]:
    """Read the vendor reported for each processor package."""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_Processor -ErrorAction Stop).Manufacturer",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.splitlines()


def cpu_vendor() -> str:
    """Require one recognized vendor across every processor on the execution host."""
    host = platform.system(), platform.machine()
    if host == ("Windows", "AMD64"):
        vendors = windows_cpu_vendors()
        if not vendors or len(set(vendors)) != 1 or vendors[0] not in VENDORS:
            raise RuntimeError("CPU vendors are missing, mixed or unsupported")
        return vendors[0]
    if host != ("Linux", "x86_64"):
        raise RuntimeError("unsupported execution platform or architecture")
    try:
        content = Path("/proc/cpuinfo").read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise RuntimeError("CPU vendor is unavailable") from error
    vendors = []
    for block in content.split("\n\n"):
        if not block.strip():
            continue
        values = [
            value.strip()
            for line in block.splitlines()
            for key, separator, value in [line.partition(":")]
            if separator and key.strip() == "vendor_id"
        ]
        if len(values) != 1 or values[0] not in VENDORS:
            raise RuntimeError("CPU vendor is missing or unsupported")
        vendors.extend(values)
    if not vendors or len(set(vendors)) != 1:
        raise RuntimeError("CPU vendors are missing or mixed")
    return vendors[0]


def validate_profile(profile: dict) -> None:
    """Refuse incomplete fixture identity, including legacy profiles without CPU provenance."""
    if not isinstance(profile, dict) or set(profile) - {"startup_tree"} != {
        "helper",
        "startup_index",
        "policy",
        "platform",
        "architecture",
        "cpu_vendor",
    }:
        raise RuntimeError("fixture profile fields differ")
    for field in (
        "helper",
        "startup_index",
        *(["startup_tree"] if "startup_tree" in profile else []),
    ):
        value = profile[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise RuntimeError("fixture digest is invalid")
    if profile["policy"] != "closed" or (profile["platform"], profile["architecture"]) not in (
        ("Linux", "x86_64"),
        ("Windows", "AMD64"),
    ):
        raise RuntimeError("unsupported fixture platform, architecture or policy")
    if profile["platform"] == "Windows" and "startup_tree" not in profile:
        raise RuntimeError("Windows qualification requires the file-router fixture")
    if profile["cpu_vendor"] not in VENDORS:
        raise RuntimeError("fixture CPU vendor is missing or unsupported")


def record_fixture(bundle: Path, producer_vendor: str, *, file_router: bool = False) -> None:
    """Record producer identity only after its fresh native startup controls succeed."""
    if cpu_vendor() != producer_vendor:
        raise RuntimeError("fixture producer CPU vendor changed")
    profile = {
        "helper": digest(helper_path(bundle, platform.system())),
        "startup_index": digest(bundle / "startup/index.json"),
        "policy": "closed",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "cpu_vendor": producer_vendor,
    }
    if file_router:
        profile["startup_tree"] = startup_identity(bundle / "startup")
    validate_profile(profile)
    (bundle / "fixture.json").write_text(
        json.dumps({"binding": binding(), "profile": profile}, indent=2) + "\n",
        encoding="utf-8",
    )


def load_fixture(bundle: Path) -> dict:
    """Require this attempt's producer record to name the downloaded helper and startup index."""
    try:
        fixture = json.loads((bundle / "fixture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("fixture CPU provenance is unavailable") from error
    if not isinstance(fixture, dict) or set(fixture) != {"binding", "profile"}:
        raise RuntimeError("fixture provenance fields differ")
    if fixture["binding"] != binding():
        raise RuntimeError("fixture belongs to another run or source")
    profile = fixture["profile"]
    validate_profile(profile)
    if profile["helper"] != digest(helper_path(bundle, profile["platform"])) or profile[
        "startup_index"
    ] != digest(bundle / "startup/index.json"):
        raise RuntimeError("fixture content differs from producer record")
    if "startup_tree" in profile and profile["startup_tree"] != startup_identity(
        bundle / "startup"
    ):
        raise RuntimeError("startup tree differs from producer record")
    return profile
