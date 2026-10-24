"""Type-check the historical MCP 1.x sample in its declared published environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from sample_blocks import declared

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples/experimental/openclaw_bicep"


def main() -> int:
    """Resolve the standalone script's pins without inheriting workspace dependencies."""
    metadata = declared(SAMPLE / "server.py")
    if metadata is None:
        raise SystemExit("The legacy MCP sample requires a PEP 723 block.")
    env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    with tempfile.TemporaryDirectory(prefix="legacy-mcp-types-") as directory:
        root = Path(directory)
        interpreter = root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(root / "env")], check=True, env=env
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(interpreter),
                *metadata["dependencies"],
                f"pyright=={version('pyright')}",
            ],
            check=True,
            env=env,
        )
        shutil.copytree(SAMPLE, root / "sample", ignore=shutil.ignore_patterns("__pycache__"))
        config = root / "pyrightconfig.json"
        config.write_text(
            json.dumps(
                {
                    "include": ["sample"],
                    "typeCheckingMode": "standard",
                    "reportUnnecessaryTypeIgnoreComment": "error",
                    "pythonVersion": "3.12",
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(interpreter),
                "-m",
                "pyright",
                "--pythonpath",
                str(interpreter),
                "--outputjson",
                "--project",
                str(config),
            ],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        if result.returncode:
            return result.returncode
        analyzed = json.loads(result.stdout)["summary"]["filesAnalyzed"]
        expected = len(list((root / "sample").rglob("*.py")))
        if analyzed < expected or not expected:
            raise SystemExit(f"Expected at least {expected} sample files, analyzed {analyzed}.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
