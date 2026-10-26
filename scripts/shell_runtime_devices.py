"""Verify the runtime supplies usable standard Linux devices to shell sandboxes."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maf_sandbox import Sandbox

DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
PROBE = """
import errno, json, os, stat
devices = ('null', 'zero', 'full', 'random', 'urandom', 'tty')
for name in devices:
    if not stat.S_ISCHR(os.stat('/dev/' + name).st_mode):
        raise RuntimeError('Not a character device: ' + name)
with open('/dev/null', 'wb', buffering=0) as stream:
    stream.write(b'probe')
with open('/dev/null', 'rb', buffering=0) as stream:
    if stream.read(1) != b'':
        raise RuntimeError('/dev/null did not return EOF')
with open('/dev/zero', 'rb', buffering=0) as stream:
    if stream.read(16) != bytes(16):
        raise RuntimeError('/dev/zero did not return zero bytes')
for name in ('random', 'urandom'):
    with open('/dev/' + name, 'rb', buffering=0) as stream:
        if len(stream.read(16)) != 16:
            raise RuntimeError('Cannot read randomness: ' + name)
with open('/dev/full', 'wb', buffering=0) as stream:
    try:
        stream.write(b'probe')
    except OSError as error:
        if error.errno != errno.ENOSPC:
            raise
    else:
        raise RuntimeError('/dev/full did not refuse the write')
print(json.dumps(list(devices)))
"""


async def check_runtime_devices(sandbox: Sandbox) -> list[str]:
    """Probe non-terminal devices; /dev/tty need not have a controlling terminal."""
    result = await sandbox.exec(
        "python3 -c " + shlex.quote(PROBE), working_directory="/", timeout=30
    )
    if result.exit_code != 0:
        raise ValueError(f"Shell runtime device probe failed: {result.stderr}")
    devices = json.loads(result.stdout)
    if devices != list(DEVICES):
        raise ValueError("Shell runtime device evidence is incomplete")
    return devices
