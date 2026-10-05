"""Measure the native renderer identity inside a provisioned Linux guest."""

import json
import os
import runpy
import sys
import tempfile
import threading
import time
from pathlib import Path


def check() -> dict[str, object]:
    """Export from a private call directory and observe Electron's actual process identities."""
    if sys.platform != "linux":
        raise RuntimeError("The renderer identity check requires a Linux guest")
    caller_uid = os.geteuid()
    expected = 10001 if caller_uid == 0 else caller_uid
    observed: set[
        tuple[str, tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]
    ] = set()
    errors: list[str] = []
    stopped = threading.Event()

    def observe() -> None:
        while not stopped.is_set():
            for process in Path("/proc").glob("[0-9]*"):
                try:
                    argv = (process / "cmdline").read_bytes().split(b"\0")
                    # Electron rewrites child argv as a single process-title string.
                    command = b" ".join(argv).split()
                    if not command or command[0] != b"/opt/drawio/drawio":
                        continue
                    fields = dict(
                        line.split(":", 1) for line in (process / "status").read_text().splitlines()
                    )
                    role = "renderer" if b"--type=renderer" in command else "electron"
                    observed.add(
                        (
                            role,
                            tuple(map(int, fields["Uid"].split())),
                            tuple(map(int, fields["Gid"].split())),
                            tuple(map(int, fields["Groups"].split())),
                            tuple(
                                int(fields[key], 16)
                                for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")
                            ),
                        )
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError as exc:
                    errors.append(str(exc))
            stopped.wait(0.005)

    runtime = runpy.run_path("/opt/maf-drawio/export.py")
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="drawio-identity-", dir="/tmp") as directory:
        os.chdir(directory)
        watcher = threading.Thread(target=observe)
        watcher.start()
        try:
            runtime["export_document"](
                '<mxfile><diagram><mxGraphModel><root><mxCell id="0"/>'
                '<mxCell id="1" parent="0"/><mxCell id="2" parent="1" vertex="1" '
                'value="Identity"><mxGeometry as="geometry" x="0" y="0" '
                'width="120" height="80"/></mxCell></root></mxGraphModel></diagram></mxfile>',
                {
                    "formats": ["svg"],
                    "pages": None,
                    "scale": 1,
                    "transparent": False,
                    "jpeg_quality": 90,
                },
                time.monotonic() + 60,
            )
            assert Path("diagram-1.svg").stat().st_uid == caller_uid
            assert json.loads(Path("exports.json").read_text())["files"] == ["diagram-1.svg"]
        finally:
            stopped.set()
            watcher.join(timeout=5)
            os.chdir(original)
    assert not watcher.is_alive() and not errors, errors
    assert {item[0] for item in observed} == {"electron", "renderer"}, observed
    for _, uids, gids, groups, capabilities in observed:
        assert uids == (expected,) * 4, observed
        assert capabilities == (0, 0, 0, 0), observed
        if caller_uid == 0:
            assert gids == (10001,) * 4 and not groups, observed
    return {
        "caller_uid": caller_uid,
        "renderer_uid": expected,
        "process_identities": sorted(observed),
    }


if __name__ == "__main__":
    print(json.dumps(check()))
