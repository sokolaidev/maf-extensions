"""Check the prebuilt runtime payload without opening a hypervisor device."""

from __future__ import annotations

import hashlib
import importlib
import json
import platform
import subprocess
from email.parser import BytesParser
from importlib.metadata import distributions, version
from pathlib import Path
from zipfile import ZipFile

PACKAGES = ("maf-sandbox", "maf-sandbox-hyperlight", "maf-sandbox-codeact")


def verify_payload(root: Path) -> dict[str, object]:
    """Check retained build inputs and installed workspace versions against their wheels."""
    manifest = root / "build-inputs.json"
    inputs = json.loads(manifest.read_bytes())
    wheels = sorted((root / "wheels").glob("*.whl"))
    expected = {
        "Dockerfile",
        ".dockerignore",
        "requirements.txt",
        "probe.py",
        "source.json",
        "verify.py",
    }
    expected.update(f"wheels/{wheel.name}" for wheel in wheels)
    if not isinstance(inputs, dict) or set(inputs) != expected or len(wheels) != len(PACKAGES):
        raise ValueError("unexpected build inputs")
    for name, digest in inputs.items():
        path = root / ("hyperlight-probe.py" if name == "probe.py" else name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"build input hash mismatch: {name}")
    installed = {}
    for wheel in wheels:
        with ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel must have exactly one package metadata record")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        name, expected_version = str(metadata["Name"]), str(metadata["Version"])
        if name not in PACKAGES or name in installed or version(name) != expected_version:
            raise ValueError(f"installed package does not match wheel: {name}")
        installed[name] = expected_version
    return {
        "build_inputs_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "source": json.loads((root / "source.json").read_bytes()),
        "workspace_packages": installed,
    }


def main() -> None:
    """Report payload integrity, dependency consistency and importability as JSON."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("the AKS runtime requires Linux x86-64")
    report = verify_payload(Path("/opt"))
    subprocess.run(
        ["python", "-I", "-m", "pip", "check"], check=True, capture_output=True, text=True
    )
    for name in (*PACKAGES, "hyperlight-sandbox"):
        importlib.import_module(name.replace("-", "_"))
    report.update(
        python=platform.python_version(),
        platform="linux/amd64",
        installed_packages={item.metadata["Name"]: item.version for item in distributions()},
        checks=["input-hashes", "workspace-versions", "pip-check", "imports"],
        hypervisor_execution_verified=False,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
