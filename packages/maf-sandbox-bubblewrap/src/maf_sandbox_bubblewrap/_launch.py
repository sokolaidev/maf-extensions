"""Enter the preconfigured resource group before starting any guest process."""

import ctypes
import os
import sys
from pathlib import Path


def main() -> None:
    """Bind lifetime to the owner, join its cgroup and replace this helper with Bubblewrap."""
    owner, group, *command = sys.argv[1:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, 9, 0, 0, 0) != 0 or os.getppid() != int(owner):
        raise RuntimeError("Could not establish owner-death supervision")
    (Path(group) / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
