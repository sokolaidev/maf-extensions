"""Measure directory archives without extracting them or trusting guest enumeration.

Requires a local Python 3 image. Only fixture creation executes guest Python; every
observation uses the engine after stopping the container. Timings are not a gate.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import PurePosixPath
from typing import IO, Any

_MIB = 1024 * 1024
_FIXTURE = """
import pathlib
root = pathlib.Path('/listing')
root.mkdir()
block = b'x' * (1024 * 1024)
for label, count, size in [
    ('empty', 0, 0), ('tiny-10', 10, 1), ('tiny-1000', 1000, 1),
    ('mib-10', 10, len(block)), ('mib-160', 10, 16 * len(block)),
    ('mib-1000', 10, 100 * len(block)),
]:
    directory = root / label
    directory.mkdir()
    for index in range(count):
        with (directory / f'{index:04d}').open('wb') as file:
            remaining = size
            while remaining:
                piece = block[:remaining]
                file.write(piece)
                remaining -= len(piece)
nested = root / 'nested'
(nested / 'a-subtree').mkdir(parents=True)
with (nested / 'a-subtree' / 'large').open('wb') as file:
    for _ in range(160):
        file.write(block)
(nested / 'z-last').write_bytes(b'z')
links = root / 'links'
links.mkdir()
(root / 'outside').mkdir()
(root / 'outside' / 'secret').write_bytes(b'outside')
(links / 'file-link').symlink_to('../outside/secret')
(links / 'dir-link').symlink_to('../outside', target_is_directory=True)
(links / 'dangling').symlink_to('../missing')
(root / 'source-link').symlink_to('links', target_is_directory=True)
(root / 'absolute-link').symlink_to('/listing/links', target_is_directory=True)
"""


def _docker(*args: str, content: bytes | None = None) -> bytes:
    return subprocess.run(
        ["docker", *args], input=content, capture_output=True, check=True, timeout=120
    ).stdout


class _CountedPipe(io.RawIOBase):
    def __init__(self, source: IO[bytes], ceiling: int | None) -> None:
        self.source = source
        self.ceiling = ceiling
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        size = len(buffer)
        if self.ceiling is not None:
            size = min(size, self.ceiling + 1 - self.count)
        data = self.source.read(size)
        self.count += len(data)
        if self.ceiling is not None and self.count > self.ceiling:
            raise ValueError("archive byte ceiling exceeded; listing is incomplete")
        buffer[: len(data)] = data
        return len(data)


def _archive(
    container: str, guest_path: str, *, follow: bool = False, ceiling: int | None = None
) -> dict[str, Any]:
    command = ["docker", "cp", *(["-L"] if follow else []), f"{container}:{guest_path}", "-"]
    started = time.perf_counter()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        assert process.stdout is not None
        counted = _CountedPipe(process.stdout, ceiling)
        timer = threading.Timer(120, process.kill)
        timer.start()
        entries: list[dict[str, Any]] = []
        try:
            with io.BufferedReader(counted) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    for member in archive:
                        entries.append(
                            {
                                "name": member.name,
                                "type": member.type.decode("ascii"),
                                "size": member.size,
                                "link": member.linkname,
                                "header_offset": member.offset,
                            }
                        )
                while stream.read(_MIB):
                    pass
            _, stderr = process.communicate(timeout=10)
            if process.returncode:
                raise RuntimeError(stderr.decode(errors="replace"))
        except tarfile.TarError as error:
            _, stderr = process.communicate(timeout=10)
            raise RuntimeError(f"{error}: {stderr.decode(errors='replace').strip()}") from error
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
        children = [entry for entry in entries if len(PurePosixPath(entry["name"]).parts) == 2]
        return {
            "path": guest_path,
            "follow": follow,
            "bytes": counted.count,
            "seconds": round(time.perf_counter() - started, 4),
            "members": len(entries),
            "children": len(children),
            "first": entries[:5],
            "last": entries[-2:],
        }


def _stop_early(container: str) -> dict[str, Any]:
    started = time.perf_counter()
    with subprocess.Popen(
        ["docker", "cp", f"{container}:/listing/mib-1000", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        timer = threading.Timer(120, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            prefix = process.stdout.read(64 * 1024)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.communicate()
    elapsed = round(time.perf_counter() - started, 4)
    next_copy = _archive(container, "/listing/empty")
    return {
        "prefix_bytes": len(prefix),
        "seconds": elapsed,
        "next_copy_seconds": next_copy["seconds"],
    }


def main() -> None:
    """Create disposable fixtures and print archive observations as JSON lines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local Linux image with Python 3")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    container = "maf-listing-measure-" + uuid.uuid4().hex[:12]
    version = json.loads(_docker("version", "--format", "{{json .}}"))
    print(
        json.dumps({"client": version["Client"]["Version"], "engine": version["Server"]["Version"]})
    )
    image_id = _docker("image", "inspect", args.image, "--format", "{{.Id}}").decode().strip()
    _docker(
        "create",
        "--name",
        container,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "python",
        image_id,
        "-c",
        "import time; time.sleep(600)",
    )
    try:
        _docker("start", container)
        _docker("exec", "-i", container, "python", "-", content=_FIXTURE.encode())
        _docker("stop", "--time", "1", container)
        for _ in range(args.repeats):
            for name in (
                "empty",
                "tiny-10",
                "tiny-1000",
                "mib-10",
                "mib-160",
                "mib-1000",
                "nested",
            ):
                print(json.dumps(_archive(container, f"/listing/{name}")), flush=True)
            print(json.dumps({"early_stop": _stop_early(container)}), flush=True)
        for guest_path in ("/listing/links", "/listing/source-link", "/listing/absolute-link"):
            for follow in (False, True):
                try:
                    result = _archive(container, guest_path, follow=follow)
                except RuntimeError as error:
                    result = {"path": guest_path, "follow": follow, "error": str(error)}
                print(json.dumps(result), flush=True)
        try:
            _archive(container, "/listing/nested", ceiling=_MIB)
        except ValueError as error:
            print(json.dumps({"bounded_listing": str(error)}), flush=True)
        else:
            raise AssertionError("an over-budget listing must refuse")
    finally:
        _docker("rm", "--force", container)


if __name__ == "__main__":
    main()
