"""Enter kernel containment before executing the native worker command."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Join the supervisor's cgroup without running host callbacks after fork."""
    directory = int(sys.argv[1])
    process = os.open("cgroup.procs", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    try:
        os.write(process, str(os.getpid()).encode("ascii"))
    finally:
        os.close(process)
        os.close(directory)
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
