"""Run offline shell-image qualification without mounting host directories into the guest."""

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    """Supply the repository's fixed test program over stdin to an isolated container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "images/shell-sandbox/test_runner.py"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--tmpfs",
            "/tmp:rw,exec,size=512m",  # nosec B108 -- guest tmpfs mount specification
            args.image,
            "/usr/local/bin/python3",
            "-",
        ],
        input=source.read_bytes(),
        check=True,
        timeout=120,
    )


if __name__ == "__main__":
    main()
