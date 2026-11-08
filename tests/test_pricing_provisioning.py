"""Extracted pricing runtimes must not retain an image user's host-side ownership."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires Linux root for archive ownership verification",
)
def test_provisioned_runtime_uses_administrator_ownership(tmp_path):
    archive = tmp_path / "runtime.tar"
    with tarfile.open(archive, "w") as output:
        directory = tarfile.TarInfo("home")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        output.addfile(directory)
        directory = tarfile.TarInfo("home/agent")
        directory.type = tarfile.DIRTYPE
        directory.uid = directory.gid = 1000
        directory.mode = 0o755
        output.addfile(directory)
        item = tarfile.TarInfo("home/agent/file")
        item.uid = item.gid = 1000
        item.size = 4
        item.mode = 0o644
        output.addfile(item, io.BytesIO(b"data"))
        link = tarfile.TarInfo("home/agent/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "file"
        link.uid = link.gid = 1000
        output.addfile(link)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'image': print('sha256:' + '0' * 64)\n"
        "elif args[0] == 'create': print('fixture-container')\n"
        "elif args[0] == 'export': sys.stdout.buffer.write(pathlib.Path(os.environ['RUNTIME_ARCHIVE']).read_bytes())\n"
        "elif args[0] == 'save': pathlib.Path(args[2]).write_bytes(b'template')\n"
        "elif args[0] == 'cp': pathlib.Path(args[2]).write_bytes(b'proxy')\n"
        "elif args[0] not in ('build', 'rm'): raise RuntimeError(args)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    target = tmp_path / "provisioned"
    subprocess.run(
        ["bash", str(ROOT / "samples/experimental/azure_pricing/provision.sh"), str(target)],
        env={
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "RUNTIME_ARCHIVE": str(archive),
        },
        check=True,
        capture_output=True,
        timeout=30,
    )
    for path in (target / "runtime").rglob("*"):
        metadata = path.lstat()
        assert metadata.st_uid == metadata.st_gid == 0
        if not path.is_symlink():
            assert metadata.st_mode & 0o222 == 0
    assert (target / "runtime/home/agent/link").read_bytes() == b"data"
