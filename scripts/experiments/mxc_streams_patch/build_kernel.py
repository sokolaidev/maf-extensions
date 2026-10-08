"""Build the experimental byte-stream kernel on a GitHub Linux runner."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.experiments.mxc_session_patch.host_call import digest
from scripts.experiments.mxc_streams_patch.kernel_patch import metadata, overlay

BUILD = r"""set -euo pipefail
mkdir -p .build app-elfloader/workdir/libs
ln -s /kernel/unikraft app-elfloader/workdir/unikraft
ln -s /kernel/libs/libelf app-elfloader/workdir/libs/libelf
ln -s /kernel/.build app-elfloader/workdir/build
cat /defconfig-elfloader > app-elfloader/.config
printf '\nCONFIG_LIBPOSIX_TTY_STDOUT_HL_BYTES=y\n# CONFIG_LIBPOSIX_TTY_STDOUT_SERIAL is not set\n' >> app-elfloader/.config
cd app-elfloader
make WITH_LWIP=n olddefconfig </dev/null
grep -qx 'CONFIG_LIBPOSIX_TTY_STDOUT_HL_BYTES=y' .config
make WITH_LWIP=n -j2
cd /kernel
chown -R "$HOST_UID:$HOST_GID" .build
"""


def run(root: Path, stage: str, *command: str) -> None:
    """Keep bounded build stages and their diagnostics independently inspectable."""
    print(f"MXC stream kernel: {stage}", flush=True)
    with (root / f"{stage}.log").open("wb") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)


def build(root: Path, report: dict) -> None:
    """Use a fresh checkout and retain the actual kernel and toolchain identities."""
    source = root / "runtime"
    run(
        root,
        "clone",
        "git",
        "clone",
        "--no-checkout",
        "https://github.com/hyperlight-dev/hyperlight-unikraft.git",
        str(source),
    )
    run(root, "checkout", "git", "-C", str(source), "checkout", metadata()["runtime_base"])
    run(
        root, "submodules", "git", "-C", str(source), "submodule", "update", "--init", "--recursive"
    )
    overlay("apply", source)
    run(
        root,
        "builder",
        "docker",
        "build",
        "--platform",
        "linux/amd64",
        "-t",
        "mxc-stream-kernel-builder",
        "-f",
        str(source / "kernel/Dockerfile.build"),
        str(source / "kernel"),
    )
    run(root, "toolchain", "docker", "run", "--rm", "mxc-stream-kernel-builder", "dpkg-query", "-W")
    run(root, "image-identity", "docker", "image", "inspect", "mxc-stream-kernel-builder")
    run(
        root,
        "compile",
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{source / 'kernel'}:/kernel",
        "-v",
        f"{source / 'defconfig-elfloader'}:/defconfig-elfloader:ro",
        "-e",
        f"HOST_UID={getattr(os, 'getuid')()}",
        "-e",
        f"HOST_GID={getattr(os, 'getgid')()}",
        "mxc-stream-kernel-builder",
        "bash",
        "-c",
        BUILD,
    )
    kernel = root / "elfloader_hyperlight-x86_64"
    shutil.copyfile(source / "kernel/.build/elfloader_hyperlight-x86_64", kernel)
    header = kernel.read_bytes()[:20]
    if header[:4] != b"\x7fELF" or header[18:20] != b"\x3e\x00":
        raise ValueError("build did not produce the expected x86-64 ELF kernel")
    report["kernel_sha256"] = digest(kernel)
    report["kernel_bytes"] = kernel.stat().st_size
    report["toolchain_sha256"] = digest(root / "toolchain.log")
    report["builder_image_id"] = json.loads(
        (root / "image-identity.log").read_text(encoding="utf-8")
    )[0]["Id"]
    overlay("remove", source)
    overlay("check", source)
    report["overlay_removed"] = True
    report["status"] = "built-not-qualified"


def main() -> int:
    """A successful kernel build alone never qualifies native stream behavior."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "unqualified",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "pins": metadata(),
    }
    try:
        if sys.platform != "linux":
            raise ValueError("build the shared kernel artifact on Linux")
        build(root, report)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["reason"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        (root / "kernel-result.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
