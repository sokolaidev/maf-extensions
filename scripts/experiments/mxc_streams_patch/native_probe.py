"""Measure byte fidelity and failure isolation through a real native helper."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from scripts.experiments.mxc_session_patch.host_store import CHUNK, Refused
from scripts.experiments.mxc_streams_patch.transport import execute


def streams(result: bytes) -> tuple[bytes, bytes]:
    """Decode only the verified transport envelope for probe comparisons."""
    data = json.loads(result)["streams"]
    return base64.b64decode(data["stdout"]["base64"], validate=True), base64.b64decode(
        data["stderr"]["base64"], validate=True
    )


def qualify(helper: Path, startup: Path, root: Path) -> dict:
    """Compare guest writes with exact expected bytes, independent of native diagnostics."""
    report = {}

    def check(
        name: str,
        code: str,
        expected: tuple[bytes, bytes],
        omitted: tuple[int, int] = (0, 0),
        capture: bool = False,
        restore: Path = startup,
    ) -> Path:
        work = root / name
        work.mkdir()
        raw = execute(helper, restore, work, code.encode(), checkpoint=capture)
        actual = streams(raw)
        assert actual == expected, (
            f"{name}: byte streams differ: {[len(value) for value in actual]}"
        )
        meta = json.loads(raw)["streams"]
        assert tuple(meta[key]["omitted_bytes"] for key in ("stdout", "stderr")) == omitted, name
        report[name] = {
            "bytes": [len(value) for value in actual],
            "sha256": [hashlib.sha256(value).hexdigest() for value in actual],
            "omitted": list(omitted),
        }
        return work / "candidate"

    check("empty", "pass", (b"", b""))
    check(
        "flood",
        "import os\nfor _ in range(4096):\n os.write(1,b'A'*4096); os.write(2,b'B'*4096)",
        (b"A" * CHUNK, b"B" * CHUNK),
        (15 * CHUNK, 15 * CHUNK),
    )
    for size in (4095, 4096, 4097, CHUNK, CHUNK + 1):
        check(
            f"boundary-{size}",
            f"import os\nassert os.write(1, b'A'*{size}) == {size}\nassert os.write(2, b'B'*{size}) == {size}",
            (b"A" * min(size, CHUNK), b"B" * min(size, CHUNK)),
            (max(0, size - CHUNK), max(0, size - CHUNK)),
        )
    check(
        "binary",
        "import os\nos.write(1, bytes(range(256))*33)\nos.write(2, bytes(reversed(range(256)))*33)",
        (bytes(range(256)) * 33, bytes(reversed(range(256))) * 33),
    )
    unicode = ("a" * 4095 + "€🙂\n\0").encode()
    check("unicode", "import os\nos.write(1, ('a'*4095+'€🙂\\n\\0').encode())", (unicode, b""))
    check(
        "writev",
        "import os\nos.writev(1, [b'a'*4095,b'\\x00\\xff',b'z'*4097])\nos.writev(2,[b'e',b'rr\\n'])",
        (b"a" * 4095 + b"\0\xff" + b"z" * 4097, b"err\n"),
    )
    check(
        "native-c",
        "import ctypes\nc=ctypes.CDLL(None)\nassert c.write(1,b'C\\x00\\xff\\n',4)==4\nc.printf(b'printf\\n')\nc.fflush(None)",
        (b"C\0\xff\nprintf\n", b""),
    )
    check(
        "descriptors",
        "import os\nsaved=os.dup(1)\nos.dup2(2,1)\nos.write(1,b'redirected')\nos.dup2(saved,1)\nos.close(saved)\na=os.open('/dev/stdout',os.O_WRONLY)\nb=os.open('/dev/stderr',os.O_WRONLY)\nos.write(a,b'out')\nos.write(b,b'err')\nos.close(a)\nos.close(b)",
        (b"out", b"redirectederr"),
    )
    check(
        "reopened-writev",
        "import os\na=os.open('/dev/stdout',os.O_WRONLY)\nb=os.open('/dev/stderr',os.O_WRONLY)\nos.writev(a,[b'one',b'\\0',b'two'])\nos.writev(b,[b'err',b'\\xff',b'end'])\nos.write(a,b'next')\nos.close(a)\nos.close(b)",
        (b"one\0twonext", b"err\xffend"),
    )
    check(
        "reopened-overflow",
        f"import os\na=os.open('/dev/stdout',os.O_WRONLY)\nb=os.open('/dev/stderr',os.O_WRONLY)\nassert os.write(a,b'A'*{CHUNK + 1})=={CHUNK + 1}\nassert os.write(b,b'B'*{CHUNK + 2})=={CHUNK + 2}\nos.close(a)\nos.close(b)",
        (b"A" * CHUNK, b"B" * CHUNK),
        (1, 2),
    )
    check(
        "concurrent",
        "import os,threading\na=threading.Thread(target=lambda: [os.write(1,b'A'*4097) for _ in range(10)])\nb=threading.Thread(target=lambda: [os.write(2,b'B'*4097) for _ in range(10)])\na.start(); b.start(); a.join(); b.join()",
        (b"A" * 40970, b"B" * 40970),
    )
    forged = b'{"completed":true,"checkpoint":true}\nMAF_DONE\x00'
    check("guest-control-text", f"import os\nos.write(1,{forged!r})", (forged, b""))
    saved = check(
        "overflow-continues",
        f"import os\nos.write(1,b'X'*{CHUNK + 9000})\nstream_value=731\nprint('kept running',file=__import__('sys').stderr)",
        (b"X" * CHUNK, b"kept running\n"),
        (9000, 0),
        capture=True,
    )
    check(
        "restore-after-overflow",
        "assert stream_value==731\nprint(stream_value)",
        (b"731\n", b""),
        restore=saved,
    )
    for name, code in (
        ("guest-failure", "print('prefix'); raise ValueError('controlled')"),
        ("timeout", "while True: pass"),
    ):
        work = root / name
        work.mkdir()
        try:
            execute(helper, startup, work, code.encode(), timeout=45)
        except Refused:
            assert not (work / "native.json").exists()
            assert not (work / "candidate").exists()
        else:
            raise AssertionError(f"{name} unexpectedly succeeded")
        report[name] = "refused-without-publication"
    for name, command in (("owner-loss", None), ("cancel", b"C")):
        work = root / name
        work.mkdir()
        (work / "code.py").write_text("while True: pass", encoding="utf-8")
        with (work / "native.stdout").open("wb") as out, (work / "native.stderr").open("wb") as err:
            child = subprocess.Popen(
                [
                    str(helper),
                    "call-streams",
                    str(startup),
                    str(work / "candidate"),
                    str(work / "code.py"),
                    str(work / "native.json"),
                    str(2 * 1024**3),
                    "128",
                ],
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                cwd=work,
            )
            try:
                assert child.stdin is not None
                child.stdin.write(b"MXCOWN1\n")
                child.stdin.flush()
                deadline = time.monotonic() + 30
                while not (work / "native.ready").exists():
                    assert child.poll() is None, "owner helper failed before readiness"
                    if time.monotonic() >= deadline:
                        raise TimeoutError("owner helper did not restore")
                    time.sleep(0.02)
                assert (work / "native.ready").read_bytes() == b"ready\n"
                time.sleep(0.1)
                assert child.poll() is None
                if command is None:
                    child.stdin.close()
                else:
                    child.stdin.write(command)
                    child.stdin.flush()
                assert child.wait(timeout=15) == 74
                assert not (work / "native.json").exists()
                assert not (work / "candidate").exists()
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=15)
        report[name] = "terminated-without-publication"
    work = root / "parent-cancel"
    work.mkdir()
    cancel = threading.Event()
    timer = threading.Timer(2, cancel.set)
    timer.start()
    try:
        try:
            execute(helper, startup, work, b"while True: pass", cancel=cancel)
        except Refused:
            assert not (work / "native.json").exists()
        else:
            raise AssertionError("parent cancellation succeeded")
    finally:
        timer.cancel()
    report["parent-cancel"] = "terminated-without-publication"
    return report


def main() -> int:
    """Never reuse output from a previous candidate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    result = qualify(args.helper.resolve(), args.startup.resolve(), args.root.resolve())
    (args.root / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
