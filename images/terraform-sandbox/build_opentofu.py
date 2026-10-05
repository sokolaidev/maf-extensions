"""Build the pinned OpenTofu source with explicit security dependency updates."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    """Verify source and module pins, test the build and retain its inputs."""
    recipe = Path(__file__).resolve().parent
    plan = json.loads((recipe / "image.json").read_text())["engines"]["opentofu"]
    source = plan["source_build"]
    checkout, output = Path("/src/opentofu"), Path("/out")
    subprocess.run(
        [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            "v" + plan["version"],
            source["repository"],
            str(checkout),
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
    ).strip()
    if revision != source["revision"]:
        raise ValueError("OpenTofu source revision mismatch")
    subprocess.run(
        ["go", "get", *(name + "@" + version for name, version in source["modules"].items())],
        cwd=checkout,
        check=True,
    )
    subprocess.run(["go", "mod", "verify"], cwd=checkout, check=True)
    for module, version in source["modules"].items():
        actual = subprocess.check_output(
            ["go", "list", "-m", "-f", "{{.Version}}", module], cwd=checkout, text=True
        ).strip()
        if actual != version:
            raise ValueError("OpenTofu security dependency mismatch")
    subprocess.run(
        ["go", "test", "./version", "./internal/command/views/json"], cwd=checkout, check=True
    )
    output.mkdir()
    subprocess.run(
        [
            "go",
            "build",
            "-trimpath",
            "-ldflags=-s -w -X github.com/opentofu/opentofu/version.dev=no",
            "-o",
            str(output / "tofu"),
            "./cmd/tofu",
        ],
        cwd=checkout,
        env={**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"},
        check=True,
    )
    for name in ("go.mod", "go.sum", "LICENSE"):
        shutil.copyfile(checkout / name, output / name)
    record = {
        "source": source,
        "go_version": subprocess.check_output(["go", "version"], text=True).strip(),
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()
        },
    }
    (output / "build.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
