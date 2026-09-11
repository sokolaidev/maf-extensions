"""What a live fixture image ships, read before a backend acquires it.

Not a test module. The live suite is skipped without a configured engine, so the probe
lives here and ``test_fixture_probe.py`` covers it where CI can run it.
"""

from __future__ import annotations

import subprocess


def _the_image_ships(guest_path: str, image: str) -> bool:
    """Whether ``image`` already carries ``guest_path``, read before any acquisition.

    Acquire prepares the base, so every fixture looks alike afterwards. Runs as root so an
    untraversable parent cannot hide the path, on no network because a path check needs
    none, and reads a word the guest prints rather than an exit code, which wslc returns
    as 1 for an unresolvable image too.
    """
    probe = subprocess.run(
        [
            "wslc",
            "container",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0",
            image,
            "sh",
            "-c",
            'if [ -e "$1" ]; then echo present; else echo absent; fi',
            "sh",
            guest_path,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    answer = probe.stdout.strip()
    if probe.returncode != 0 or answer not in ("present", "absent"):
        raise RuntimeError(
            f"probing {image} for {guest_path} exited {probe.returncode} with {answer!r}:"
            f" {probe.stderr}"
        )
    return answer == "present"
