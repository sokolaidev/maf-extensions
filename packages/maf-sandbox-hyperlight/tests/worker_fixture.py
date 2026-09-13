"""A subprocess fixture for pipe pressure and termination; it never evaluates guest source."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    """Serve the test protocol or stall at the selected operation."""
    mode = sys.argv[1]
    for line in sys.stdin.buffer:
        request = json.loads(line)
        operation = request["op"]
        code = request.get("code")
        if mode == f"hang_{operation}" or code == "hang":
            time.sleep(60)
        if mode == "fail_init" and operation == "init":
            sys.stderr.write("startup refused")
            return
        if code == "diagnostics":
            sys.stderr.buffer.write(b"d" * (4 * 1024 * 1024))
            sys.stderr.buffer.flush()
        if code == "oversize":
            sys.stdout.buffer.write(b"x" * (4 * 1024 * 1024))
            sys.stdout.buffer.flush()
            time.sleep(60)
        if code == "die":
            os._exit(17)
        if code == "limits":
            result = {"error": "output_limit"}
        elif operation == "run":
            result = {"stdout": code, "stderr": "", "exit_code": 0}
        else:
            result = {"ok": True}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
        if operation == "init" and mode == "stop_reading":
            time.sleep(60)


if __name__ == "__main__":
    main()
