"""Build-time downloads with fixed archive checksums; never used at validation time."""

import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ENGINES = {
    "terraform": (
        "1.16.2",
        "terraform",
        "https://releases.hashicorp.com/terraform/1.16.2/terraform_1.16.2_linux_amd64.zip",
        "0d17011f0c4664539b164b044903d04e296c86c13cb9f28040076c65cfb3985a",
    ),
    "opentofu": (
        "1.12.6",
        "tofu",
        "https://github.com/opentofu/opentofu/releases/download/v1.12.6/"
        "tofu_1.12.6_linux_amd64.zip",
        "5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8",
    ),
}
PROVIDERS = {
    "terraform": (
        "registry.terraform.io",
        "https://releases.hashicorp.com/terraform-provider-random/3.7.2/"
        "terraform-provider-random_3.7.2_linux_amd64.zip",
        "7b8434212eef0f8c83f5a90c6d76feaf850f6502b61b53c329e85b3b281cba34",
    ),
    "opentofu": (
        "registry.opentofu.org",
        "https://github.com/opentofu/terraform-provider-random/releases/download/v3.7.2/"
        "terraform-provider-random_3.7.2_linux_amd64.zip",
        "9b0ac4c1d8e36a86b59ced94fa517ae9b015b1d044b3455465cc6f0eab70915d",
    ),
}


def download(url: str, digest: str) -> bytes:
    """Verify archive identity before extracting or installing it."""
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("archive checksum mismatch")
    return data


def main(engine: str, profile: str) -> None:
    """Install one engine and optionally its registry's pinned random provider."""
    version, executable, url, digest = ENGINES[engine]
    destination = Path("/opt/maf-terraform")
    destination.mkdir(parents=True, exist_ok=True)
    archive = download(url, digest)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        binary = bundle.read(executable)
        path = Path("/usr/local/bin") / executable
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
    (destination / "engine.json").write_text(
        json.dumps(
            {
                "engine": engine,
                "version": version,
                "archive_sha256": digest,
                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                "profile": profile,
            }
        )
    )
    mirror = destination / "mirror"
    mirror.mkdir(exist_ok=True)
    if profile == "random":
        registry, provider_url, provider_digest = PROVIDERS[engine]
        package = mirror / registry / "hashicorp" / "random"
        package.mkdir(parents=True)
        (package / "terraform-provider-random_3.7.2_linux_amd64.zip").write_bytes(
            download(provider_url, provider_digest)
        )
    elif profile != "builtin":
        raise ValueError("profile must be builtin or random")
    (destination / "terraform.rc").write_text(
        "disable_checkpoint = true\nprovider_installation {\n"
        '  filesystem_mirror { path = "/opt/maf-terraform/mirror" }\n}\n'
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
