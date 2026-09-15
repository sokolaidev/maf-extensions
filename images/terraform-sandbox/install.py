"""Build-time downloads with fixed archive checksums; never used at validation time."""

import hashlib
import io
import json
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    """Refuse ambiguous image configuration and provider approvals."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def load_plan(engine: str, profile: str, config_path: Path | None = None) -> dict[str, Any]:
    """Read engine pins and the selected provider profile before downloading anything."""
    config_path = config_path or Path(__file__).with_name("image.json")
    config = load_json(config_path)
    if config["schema"] != 1 or config["platform"] != "linux/amd64":
        raise ValueError("unsupported image build configuration")
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", config["base_image"]):
        raise ValueError("base image must be pinned by digest")
    if engine not in {"terraform", "opentofu"}:
        raise ValueError("unsupported engine")
    selected = config["engines"][engine]
    if selected["executable"] != ("terraform" if engine == "terraform" else "tofu"):
        raise ValueError("engine executable mismatch")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", selected["version"]):
        raise ValueError("engine version must be explicit")
    if not selected["url"].startswith("https://") or not re.fullmatch(
        r"[0-9a-f]{64}", selected["sha256"]
    ):
        raise ValueError("engine archive needs an HTTPS URL and SHA-256")
    if profile not in selected["profiles"]:
        raise ValueError("unsupported provider profile")
    manifest_name = selected["profiles"][profile]
    providers: list[dict[str, Any]] = []
    if manifest_name is not None:
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.json", manifest_name):
            raise ValueError("provider manifest must be a sibling JSON file")
        manifest = load_json(config_path.with_name(manifest_name))
        if manifest["schema"] != 1 or manifest["engine"] != engine:
            raise ValueError("provider manifest engine mismatch")
        providers = manifest["providers"]
        identities: set[tuple[str, str, str]] = set()
        for provider in providers:
            if (
                not re.fullmatch(
                    r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}/[a-z0-9-]+/[a-z0-9-]+",
                    provider["source"],
                )
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", provider["version"])
                or provider["platform"] != config["platform"].replace("/", "_")
                or not provider["artifact"]["url"].startswith("https://")
                or not re.fullmatch(r"[0-9a-f]{64}", provider["artifact"]["sha256"])
            ):
                raise ValueError("unsupported provider artifact")
            identity = (provider["source"], provider["version"], provider["platform"])
            if identity in identities:
                raise ValueError("duplicate provider identity")
            identities.add(identity)
    return {
        **selected,
        "base_image": config["base_image"],
        "platform": config["platform"],
        "providers": providers,
    }


def download(url: str, digest: str) -> bytes:
    """Verify archive identity before extracting or installing it."""
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("archive checksum mismatch")
    return data


def main(
    engine: str,
    profile: str,
    expected_version: str,
    *,
    config_path: Path | None = None,
    destination: Path = Path("/opt/maf-terraform"),
    bin_directory: Path = Path("/usr/local/bin"),
) -> None:
    """Install inside the image build, refusing labels that disagree with the binary version."""
    plan = load_plan(engine, profile, config_path)
    version, executable = plan["version"], plan["executable"]
    if expected_version != version:
        raise ValueError("image version metadata must match image.json")
    url, digest = plan["url"], plan["sha256"]
    destination.mkdir(parents=True, exist_ok=True)
    archive = download(url, digest)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        binary = bundle.read(executable)
        path = bin_directory / executable
        path.write_bytes(binary)
        path.chmod(0o755)
        notices = destination / "licenses"
        notices.mkdir(exist_ok=True)
        for entry in bundle.namelist():
            if (
                "LICENSE" in entry.upper()
                or "NOTICE" in entry.upper()
                or "COPYING" in entry.upper()
            ):
                (notices / Path(entry).name).write_bytes(bundle.read(entry))
    reported = subprocess.run(
        [str(path), "version", "-json"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "CHECKPOINT_DISABLE": "1"},
        capture_output=True,
        check=True,
        timeout=30,
    )
    if json.loads(reported.stdout)["terraform_version"] != version:
        raise ValueError("downloaded binary version does not match image.json")
    (destination / "engine.json").write_text(
        json.dumps(
            {
                "engine": engine,
                "version": version,
                "executable": executable,
                "platform": plan["platform"],
                "archive_sha256": digest,
                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                "profile": profile,
            }
        )
    )
    mirror = destination / "mirror"
    mirror.mkdir(exist_ok=True)
    for provider in plan["providers"]:
        package = mirror / provider["source"]
        package.mkdir(parents=True, exist_ok=True)
        name = provider["source"].split("/")[-1]
        filename = f"terraform-provider-{name}_{provider['version']}_{provider['platform']}.zip"
        (package / filename).write_bytes(
            download(provider["artifact"]["url"], provider["artifact"]["sha256"])
        )
    (destination / "terraform.rc").write_text(
        "disable_checkpoint = true\nprovider_installation {\n"
        '  filesystem_mirror { path = "/opt/maf-terraform/mirror" }\n}\n'
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
