"""Wait for the immutable ownership binding before starting the application."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Keep the application PID stable across the supervised startup handshake."""
    descriptor = int(sys.argv[1])
    if os.read(descriptor, 1) != b"1":
        raise RuntimeError("pod supervisor did not publish ownership")
    os.close(descriptor)
    command = sys.argv[2:]
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
