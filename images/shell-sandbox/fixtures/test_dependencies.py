"""Qualify pinned host modules and assemblies in the derived fixture image."""

import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """Verify host module discovery succeeds while submitted modules cannot shadow it."""
    marker = Path(tempfile.gettempdir()) / "maf-shell-host-module-loaded"
    marker.unlink(missing_ok=True)
    sources = [
        {"path": "main.ps1", "text": "HostProbe\\Get-HostValue -Name hello", "dialect": "pwsh"},
        {
            "path": "HostProbe.psm1",
            "text": "throw 'submitted module was executed'",
            "dialect": "pwsh",
        },
        {
            "path": "assembly.ps1",
            "text": (
                "using assembly /opt/maf-shell/dependencies/assemblies/HostProbe.dll\n"
                "param([HostProbeType]$Value)\nWrite-Output $Value"
            ),
            "dialect": "pwsh",
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "request.json").write_text(
            json.dumps({"flavor": "pwsh", "files": sources})
        )
        result = subprocess.run(
            ["python3", "-I", "/opt/maf-shell/runner.py", "30"],
            cwd=directory,
            capture_output=True,
            check=True,
            timeout=35,
        )
        report = json.loads(result.stdout)
        assert report["error"] is None, result.stdout
        assert all(item["parsed"] for item in report["files"]), result.stdout
        assert all(
            item["host_assemblies"] == ["/opt/maf-shell/dependencies/assemblies/HostProbe.dll"]
            for item in report["files"]
        )
        assert marker.read_text() == "host"
        assert not (Path(directory) / "request.json").exists()
    print("Pinned host module discovery, assembly reference and submitted-module shadowing passed")


if __name__ == "__main__":
    main()
