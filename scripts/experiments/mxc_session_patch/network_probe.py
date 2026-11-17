"""Qualify socket denial through Python and libc before and after native state restoration."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from maf_sandbox_mxc.files.request import FileLimits, Request
from maf_sandbox_mxc.files.transport import execute as execute_files
from maf_sandbox_mxc.host_call import digest, execute

CASES = {
    f"{api}-{family}-{kind}": (family, kind)
    for api in ("socket", "_socket", "libc")
    for family in (2, 10)
    for kind in (1, 2, 3)
}
CODE = """import socket, _socket, ctypes, errno, json
libc = ctypes.CDLL(None, use_errno=True)
libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
libc.socket.restype = ctypes.c_int
libc.close.argtypes = [ctypes.c_int]
denials = {}
for api in ('socket', '_socket', 'libc'):
    for family in (2, 10):
        for kind in (1, 2, 3):
            key = '%s-%d-%d' % (api, family, kind)
            if api == 'libc':
                ctypes.set_errno(0)
                fd = libc.socket(family, kind, 0)
                error = ctypes.get_errno()
                if fd >= 0:
                    libc.close(fd)
                    raise AssertionError('network socket admitted: ' + key)
            else:
                try:
                    channel = (socket if api == 'socket' else _socket).socket(family, kind)
                except OSError as refused:
                    error = refused.errno
                else:
                    channel.close()
                    raise AssertionError('network socket admitted: ' + key)
            assert error in (errno.EACCES, errno.EPERM), (key, error)
            denials[key] = error
_mxc_network_sequence = globals().get('_mxc_network_sequence', 0) + 1
print(json.dumps({'sequence': _mxc_network_sequence, 'denials': denials}, sort_keys=True))
"""


def validate(payload: bytes, sequence: int) -> dict[str, int]:
    """Require the complete denial matrix and proof that the selected state was restored."""
    record = json.loads(payload)
    if not isinstance(record, dict) or set(record) != {"sequence", "denials"}:
        raise ValueError("invalid network qualification record")
    denials = record["denials"]
    if (
        type(record["sequence"]) is not int
        or record["sequence"] != sequence
        or not isinstance(denials, dict)
        or set(denials) != set(CASES)
        or any(type(value) is not int or value not in (1, 13) for value in denials.values())
    ):
        raise ValueError("network denial or restoration contract differs")
    return denials


def qualify(helper: Path, startup: Path, root: Path, profile: str) -> dict:
    """Run a fixed program only; an admitted socket fails before any outbound operation."""
    if profile not in {"console", "files"}:
        raise ValueError("unknown native transport profile")
    root.mkdir(parents=True, exist_ok=False)
    report = {"profile": profile, "helper_sha256": digest(helper), "boundaries": {}}
    for sequence, boundary in enumerate(("fresh", "restored"), start=1):
        work = root / boundary
        work.mkdir()
        if profile == "files":
            raw = execute_files(
                helper,
                startup,
                work,
                Request(CODE.encode(), (), (), FileLimits()),
                restoring=sequence == 2,
            )
            result = json.loads(raw)["streams"]
            payload = base64.b64decode(result["stdout"]["base64"], validate=True)
            if base64.b64decode(result["stderr"]["base64"], validate=True):
                raise ValueError("unexpected guest stderr")
        else:
            result = json.loads(execute(helper, startup, work, CODE.encode(), output_limit=8192))
            payload = base64.b64decode(result["console_base64"], validate=True)
        if (work / "native.stdout").read_bytes():
            raise ValueError("unexpected native output echo")
        report["boundaries"][boundary] = validate(payload, sequence)
        startup = work / "candidate"
    report["status"] = "qualified"
    (root / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    """Check an operator-selected helper without enabling any guest network authority."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", choices=("console", "files"), required=True)
    args = parser.parse_args()
    qualify(args.helper.resolve(), args.startup.resolve(), args.root.resolve(), args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
