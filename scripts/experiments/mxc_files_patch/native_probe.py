"""Qualify file confinement, lifecycle, capacity errors and capture in native guests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path

from scripts.experiments.mxc_files_patch.request import FileLimits, Input, Request, WorkspaceLimits
from scripts.experiments.mxc_files_patch.transport import execute
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_streams_patch.native_probe import streams

IO = b"""def read_file(path):
 with open(path,'rb') as stream: return stream.read()
def write_file(path, data):
 with open(path,'wb') as stream: return stream.write(data)
def append_file(path, data):
 with open(path,'ab') as stream: return stream.write(data)
"""


def qualify(helper: Path, startup: Path, root: Path) -> dict:
    """Require exact bytes and explicit refusals from independent helper processes."""
    report = {}

    def check(
        name: str,
        code: str,
        *,
        inputs: tuple[Input, ...] = (),
        artifacts: Mapping[str, bytes | None] | None = None,
        workspace: WorkspaceLimits = WorkspaceLimits(),
        limits: FileLimits = FileLimits(),
        restore: Path | None = None,
        capture: bool = False,
        refused: bool = False,
        match_checkpoint: bool = False,
    ) -> Path:
        work = root / name
        work.mkdir()
        wanted = artifacts or {}
        request = Request(IO + code.encode(), inputs, tuple(wanted), limits, workspace)
        try:
            raw = execute(
                helper,
                restore or startup,
                work,
                request,
                checkpoint=capture,
                restoring=restore is not None,
            )
        except Refused:
            if not refused:
                raise
            assert not (work / "native.json").exists()
            assert not (work / "candidate").exists()
            report[name] = {"refused_without_publication": True}
            return work / "candidate"
        assert not refused, name
        assert streams(raw) == (b"ok\n", b""), (name, streams(raw))
        actual = {
            item["name"]: base64.b64decode(item["base64"], validate=True)
            for item in json.loads(raw)["artifacts"]
        }
        assert set(actual) == set(wanted), name
        for key, expected in wanted.items():
            if expected is None:
                if key.endswith(".png"):
                    assert actual[key].startswith(b"\x89PNG\r\n\x1a\n"), name
                else:
                    assert len(actual[key]) == 65537, name
            else:
                assert actual[key] == expected, name
        if match_checkpoint:
            meta = json.loads((work / "candidate/workspace.json").read_text(encoding="utf-8"))
            data = (work / "candidate/workspace.bin").read_bytes()
            for key, value in actual.items():
                entries = [e for e in meta["files"] if e["name"].endswith("/" + key)]
                assert len(entries) == 1
                entry = entries[0]
                assert data[entry["offset"] : entry["offset"] + entry["bytes"]] == value
        report[name] = {
            "result_sha256": hashlib.sha256(raw).hexdigest(),
            "artifacts": {k: hashlib.sha256(v).hexdigest() for k, v in actual.items()},
        }
        return work / "candidate"

    binary = bytes(range(256)) * 257
    saved = check(
        "input-lifecycle",
        r"""assert read_file('nested/data.bin')==bytes(range(256))*257
append_file('nested/data.bin',b'!')
append_file(guest_session_path+'/keep',b'-guest')
old_call_path=guest_call_path
old_call_fd=os.open('nested/data.bin',os.O_RDONLY)
kept_fd=os.open(guest_session_path+'/keep',os.O_RDONLY)
print('ok')""",
        inputs=(Input("nested/data.bin", binary), Input("keep", b"host", "session")),
        artifacts={"nested/data.bin": binary + b"!"},
        capture=True,
    )
    check(
        "restore-session-and-reclaim-call",
        r"""assert guest_call_path != old_call_path
assert not os.path.exists(old_call_path+'/nested/data.bin')
assert read_file(guest_session_path+'/keep')==b'host-guest'
assert os.read(kept_fd,100)==b'host-guest'
try: os.read(old_call_fd,10)
except OSError: pass
else: raise AssertionError('expired descriptor still readable')
print('ok')""",
        restore=saved,
    )
    check(
        "replacement-refused",
        "print('ok')",
        inputs=(Input("keep", b"new", "session"),),
        restore=saved,
        refused=True,
    )
    replaced = check(
        "replacement-explicit",
        "assert read_file(guest_session_path+'/keep')==b'new'; print('ok')",
        inputs=(Input("keep", b"new", "session", True),),
        restore=saved,
        capture=True,
    )
    check(
        "replacement-restored",
        "assert read_file(guest_session_path+'/keep')==b'new'; print('ok')",
        restore=replaced,
    )
    check(
        "call-replaces-session",
        "assert read_file('keep')==b'temporary'; assert not os.path.exists(guest_session_path+'/keep'); print('ok')",
        inputs=(Input("keep", b"temporary", "call", True),),
        restore=saved,
    )
    check(
        "whole-write-quota",
        r"""import errno
f=os.open('f',os.O_RDWR)
for write in (lambda:os.write(f,b'z'*65538),lambda:os.writev(f,[b'z'*32768,b'z'*32770])):
 try: write()
 except OSError as e: assert e.errno==errno.ENOSPC
 else: raise AssertionError('oversized operation accepted')
 assert os.lseek(f,0,os.SEEK_CUR)==0
 assert read_file('f')==b'old'
assert os.writev(f,[b'a'*32768,b'b'*32769])==65537
try: os.ftruncate(f,65538)
except OSError as e: assert e.errno==errno.ENOSPC
else: raise AssertionError('oversized resize accepted')
assert os.fstat(f).st_size==65537
os.close(f)
print('ok')""",
        inputs=(Input("f", b"old"),),
        artifacts={"f": b"a" * 32768 + b"b" * 32769},
        workspace=WorkspaceLimits(files=2, bytes=65537),
    )
    check(
        "append-sparse-count",
        r"""import errno
f=os.open('f',os.O_RDWR)
os.lseek(f,4,os.SEEK_SET); assert os.write(f,b'z')==1; os.close(f)
f=os.open('f',os.O_WRONLY|os.O_APPEND); assert os.write(f,b'!')==1; os.close(f)
try: open('extra','wb')
except OSError as e: assert e.errno==errno.ENOSPC
else: raise AssertionError('file count exceeded')
print('ok')""",
        inputs=(Input("f", b"a"),),
        artifacts={"f": b"a\x00\x00\x00z!"},
        workspace=WorkspaceLimits(files=1, bytes=6),
    )
    check(
        "links-special-and-paths",
        r"""import errno
for action in (lambda:os.symlink('/etc/passwd','link'),lambda:os.link('f','link'),lambda:os.mkfifo('pipe')):
 try: action()
 except OSError: pass
 else: raise AssertionError('unsafe file accepted')
assert not os.path.exists('link')
assert not os.path.exists('pipe')
assert read_file('f')==b'safe'
print('ok')""",
        inputs=(Input("f", b"safe"),),
        artifacts={"f": b"safe"},
    )
    for name, code, artifacts, limits in (
        (
            "missing-artifact",
            "write_file(guest_session_path+'/keep',b'bad')",
            {"missing": b""},
            FileLimits(),
        ),
        (
            "oversized-artifact",
            "write_file('out',b'12345')",
            {"out": b""},
            FileLimits(file_bytes=4),
        ),
        ("directory-artifact", "os.mkdir('out')", {"out": b""}, FileLimits()),
    ):
        check(
            name,
            code,
            artifacts=artifacts,
            limits=limits,
            restore=saved,
            refused=True,
            capture=True,
        )
    check(
        "failure-keeps-checkpoint",
        "assert read_file(guest_session_path+'/keep')==b'host-guest'; print('ok')",
        restore=saved,
    )
    check(
        "rename-and-list",
        r"""os.mkdir('dir'); os.rename('f','dir/moved')
assert os.listdir('dir')==['moved']
assert read_file('dir/moved')==b'move'
print('ok')""",
        inputs=(Input("f", b"move"),),
        artifacts={"dir/moved": b"move"},
    )
    check(
        "text",
        "s=read_file('text.txt').decode(); write_file('out.txt',s.upper().encode()); print('ok')",
        inputs=(Input("text.txt", "hello €\n".encode()),),
        artifacts={"out.txt": "HELLO €\n".encode()},
    )
    # A byte-exact data artifact accompanies an image whose encoding is checked in the guest.
    check(
        "csv-to-chart",
        r"""import pandas as pd,matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
frame=pd.read_csv('data.csv'); assert frame['value'].sum()==7
frame.plot(x='name',y='value',kind='bar'); plt.savefig('chart.png'); plt.close()
assert read_file('chart.png')[:8]==b'\x89PNG\r\n\x1a\n'
write_file('summary.txt',str(frame['value'].sum()).encode())
print('ok')""",
        inputs=(Input("data.csv", b"name,value\na,3\nb,4\n"),),
        artifacts={"summary.txt": b"7", "chart.png": None},
    )
    check(
        "background-capture",
        r"""import threading,time
os.rename(guest_session_path+'/changing.bin','changing.bin')
background_fd=os.open('changing.bin',os.O_RDWR)
def change_file():
 i=0
 while True:
  os.pwrite(background_fd,bytes([i%256])*65537,0)
  i+=1
  time.sleep(0.002)
threading.Thread(target=change_file,daemon=True).start()
time.sleep(0.02)
print('ok')""",
        inputs=(Input("changing.bin", b"a" * 65537, "session"),),
        artifacts={"changing.bin": None},
        capture=True,
        match_checkpoint=True,
    )
    for name, code in (
        ("guest-error", "write_file('out',b'bad'); raise ValueError('controlled')"),
        ("deadline", "write_file('out',b'bad')\nwhile True: pass"),
        ("cancel", "write_file('out',b'bad')\nwhile True: pass"),
    ):
        work = root / name
        work.mkdir()
        cancel = threading.Event()
        timer = threading.Timer(3, cancel.set)
        if name == "cancel":
            timer.start()
        try:
            try:
                execute(
                    helper,
                    startup,
                    work,
                    Request(IO + code.encode(), (), ("out",), FileLimits()),
                    timeout=15,
                    cancel=cancel,
                )
            except Refused:
                assert not (work / "native.json").exists()
                assert not (work / "candidate").exists()
            else:
                raise AssertionError(f"{name} published success")
        finally:
            timer.cancel()
        report[name] = {"refused_without_publication": True}
    return report


def main() -> int:
    """Write success evidence only after every native assertion passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("helper", "startup", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    report = qualify(args.helper.resolve(), args.startup.resolve(), args.root.resolve())
    (args.root / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
