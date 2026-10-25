"""Install only digest-verified analyzer archives during the image build."""

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def install_apk(component: str, version: str, digest: str) -> None:
    """Authenticate the repository package and verify its bytes before offline installation."""
    with tempfile.TemporaryDirectory() as cache:
        apk = ["/usr/bin/apk", "--cache-dir", cache]
        package = f"{component}={version}"
        subprocess.run([*apk, "update"], check=True)
        subprocess.run([*apk, "cache", "download", package], check=True)
        archives = list(Path(cache).glob(f"{component}-{version}.*.apk"))
        if len(archives) != 1 or hashlib.sha256(archives[0].read_bytes()).hexdigest() != digest:
            raise ValueError("cached component digest mismatch")
        subprocess.run([*apk, "--no-network", "add", package], check=True)


def install(component: str, destination: Path) -> None:
    """Verify the complete archive before extracting it into the image."""
    spec = json.loads(Path("/recipe/image.json").read_text())[component]
    if urllib.parse.urlsplit(spec["url"]).scheme != "https":
        raise ValueError("component URLs must use HTTPS")
    with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as temporary:
        archive = Path(temporary.name)
        # Only digest-verified HTTPS artifacts from the host-owned recipe reach extraction.
        with urllib.request.urlopen(spec["url"], timeout=120) as response:  # nosec B310
            shutil.copyfileobj(response, temporary)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError("component digest mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    if component in {"bash", "dash"}:
        install_apk(component, spec["version"], spec["sha256"])
    elif component == "psscriptanalyzer":
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError("archive path escape")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as content, target.open("wb") as output:
                        shutil.copyfileobj(content, output)
    else:
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")
    if component == "shellcheck":
        retained = Path("/usr/share/maf-shell/shellcheck-source.tar.gz")
        retained.parent.mkdir(parents=True, exist_ok=True)
        archive.rename(retained)
    else:
        archive.unlink()


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]))
