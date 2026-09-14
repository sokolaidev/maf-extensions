"""Run Linux worker or live KVM tests as the sudo caller in a delegated cgroup v2 tree."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

_CHILD = """import grp, os, pwd, sys
account = pwd.getpwuid(int(sys.argv[1]))
with open(sys.argv[2], 'w') as stream:
    stream.write(str(os.getpid()))
groups = os.getgrouplist(account.pw_name, account.pw_gid)
try:
    groups.append(grp.getgrnam('kvm').gr_gid)
except KeyError:
    pass
os.setgroups(groups)
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
os.environ['HOME'] = account.pw_dir
os.environ['USER'] = os.environ['LOGNAME'] = account.pw_name
os.execv(sys.argv[3], sys.argv[3:])
"""


def main() -> int:
    """Provision only the test subtree and remove it after the unprivileged test process exits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="enable real KVM guest execution")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("the cgroup test helper requires Linux")
        return 2
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        parser.error("run with sudo on Linux, preserving SUDO_UID for the unprivileged test owner")
    uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    if uid == 0:
        parser.error("the test owner must be unprivileged")
    root = Path("/sys/fs/cgroup") / ("maf-hyperlight-tests-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        (root / "cgroup.subtree_control").write_text("+memory", encoding="ascii")
        host = root / "host"
        host.mkdir()
        for path in (root, root / "cgroup.procs"):
            os.chown(path, uid, gid)
        environment = dict(os.environ)
        environment.update(MAF_HYPERLIGHT_LINUX_TESTS="1", MAF_HYPERLIGHT_CGROUP_ROOT=str(root))
        if args.live:
            environment["MAF_HYPERLIGHT_LIVE"] = "1"
        else:
            environment.pop("MAF_HYPERLIGHT_LIVE", None)
        suite = "packages/maf-sandbox-hyperlight/tests"
        test_args = args.pytest_args or [
            "-q",
            suite + "/test_hyperlight_live.py" if args.live else suite,
        ]
        if test_args[0] == "--":
            test_args = test_args[1:]
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _CHILD,
                str(uid),
                str(host / "cgroup.procs"),
                str(args.python.absolute()),
                "-m",
                "pytest",
                *test_args,
            ],
            env=environment,
            check=False,
        ).returncode
    finally:
        (root / "cgroup.kill").write_text("1", encoding="ascii")
        deadline = time.monotonic() + 5
        while "populated 1" in (root / "cgroup.events").read_text(encoding="ascii"):
            if time.monotonic() >= deadline:
                raise RuntimeError("test cgroup did not empty; cleanup could not be confirmed")
            time.sleep(0.01)
        for path in sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
        root.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
