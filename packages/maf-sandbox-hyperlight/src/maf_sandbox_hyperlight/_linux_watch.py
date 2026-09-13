"""Watch kernel process handles outside the native worker's memory cgroup."""

from __future__ import annotations

import os
import select
import sys
import time

from ._linux import kill_group
from ._wire import HyperlightWorkerError


def main() -> None:
    """Terminate the worker tree when either its owner or its root worker exits."""
    owner, worker, directory, parent = map(int, sys.argv[1:5])
    name, timeout = sys.argv[5], float(sys.argv[6])
    poller = select.poll()
    poller.register(owner, select.POLLIN)
    poller.register(worker, select.POLLIN)
    try:
        os.write(sys.stdout.fileno(), b"1")
        poller.poll()
    finally:
        # A new owner must not acquire the lock while cleanup is still unconfirmed.
        while True:
            try:
                kill_group(directory, parent, name, time.monotonic() + timeout)
            except (OSError, HyperlightWorkerError):
                time.sleep(0.1)
            else:
                break


if __name__ == "__main__":
    main()
