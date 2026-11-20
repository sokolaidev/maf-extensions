"""Qualify call-boundary flushing and closed networking on the upgraded guest."""

from __future__ import annotations

import argparse
import base64
import json
import shutil
from pathlib import Path

from maf_sandbox_mxc.host_call import execute

CASES = {
    "stdout-no-newline": ("print('flushed', end='')", b"flushed"),
    "stderr-no-newline": ("import sys; sys.stderr.write('error')", b"error"),
    "split-utf8": (
        "import os; os.write(1, b'\\xe2'); os.write(1, b'\\x82\\xac')",
        "€".encode(),
    ),
    "closed-network": (
        """import errno, socket
for family in (socket.AF_INET, socket.AF_INET6):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        try:
            sock = socket.socket(family, kind)
        except OSError as error:
            assert error.errno == errno.EACCES, error
        else:
            sock.close()
            raise AssertionError('closed network admitted a socket')
print('closed', end='')
""",
        b"closed",
    ),
}


def qualify(helper: Path, startup: Path, root: Path) -> dict:
    """Require captured bytes and native socket denial without attempting outbound traffic."""
    root.mkdir(parents=True, exist_ok=False)
    results = {}
    for name, (code, expected) in CASES.items():
        case = root / name
        case.mkdir()
        result = json.loads(execute(helper, startup, case, code.encode(), output_limit=64))
        payload = base64.b64decode(result["console_base64"], validate=True)
        metadata = result["output"]
        if payload != expected or (case / "native.stdout").read_bytes():
            raise RuntimeError(f"{name}: capture or echo contract differs")
        if metadata != {
            "limit_bytes": 64,
            "retained_bytes": len(expected),
            "omitted_bytes": 0,
            "omitted_bytes_saturated": False,
            "truncated": False,
        }:
            raise RuntimeError(f"{name}: output metadata differs")
        results[name] = metadata
        shutil.rmtree(case / "candidate")
    (root / "result.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def main() -> int:
    """Exercise only an operator-selected helper and its matching startup checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.helper.resolve(), args.startup.resolve(), args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
