"""Fixed offline ClamAV preparation and scan program, executed only in the guest."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

SEED = Path("/opt/maf-antivirus/seed")
DATABASES = ("main", "daily", "bytecode")
MAX_DIAGNOSTIC = 8192


class Refused(ValueError):
    """A database or scan policy prevents an acceptance verdict."""


def run(command: list[str], deadline: float, *, cwd: Path | None = None) -> tuple[int, str]:
    """Keep engine diagnostics out of the structured response and bound retained output."""
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=max(0.001, deadline - time.monotonic()),
            check=False,
            env=os.environ | {"LC_ALL": "C"},
        )
        output.seek(0)
        return result.returncode, output.read(MAX_DIAGNOSTIC + 1).decode("utf-8", "replace")


def checked(command: list[str], deadline: float, *, cwd: Path | None = None) -> None:
    """Require native signature validation and patch application to succeed."""
    code, output = run(command, deadline, cwd=cwd)
    if code:
        raise ValueError(output[:MAX_DIAGNOSTIC])


def header(path: Path) -> dict[str, int]:
    """Read bounded CVD or authenticated post-patch INFO metadata."""
    with path.open("rb") as stream:
        fields = stream.read(512).split(b"\n", 1)[0].strip().decode("ascii").split(":")
    if len(fields) != 9 or fields[0] != "ClamAV-VDB":
        raise ValueError("invalid database header")
    result = {"version": int(fields[2]), "built": int(fields[8]), "flevel": int(fields[4])}
    if any(value < 0 for value in result.values()) or result["version"] == 0:
        raise ValueError("invalid database metadata")
    return result


def digest(path: Path) -> str:
    """Hash an update or seed without loading a full database into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def seed() -> dict[str, Any]:
    """Identify all immutable image databases before the provider selects updates."""
    return {
        name: header(SEED / f"{name}.cvd") | {"sha256": digest(SEED / f"{name}.cvd")}
        for name in DATABASES
    }


def prepare(request: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Authenticate the seed, then apply ordered signed updates in private directories."""
    import ctypes

    engine = ctypes.CDLL("libclamav.so.12")
    engine.cl_retflevel.restype = ctypes.c_uint
    flevel = engine.cl_retflevel()
    manifest = request["update"]
    actual = seed()
    for name in DATABASES:
        expected = manifest["seed"][name]
        if any(actual[name][field] != expected[field] for field in ("version", "sha256")):
            raise ValueError("seed identity differs")
    total = 0
    for index, item in enumerate(manifest["files"]):
        if (
            re.fullmatch(r"(main|daily|bytecode)(\.cvd|-[1-9][0-9]{0,9}\.cdiff)", item["name"])
            is None
        ):
            raise ValueError("invalid update filename")
        total += item["size"]
        if not 0 < item["size"] <= 95 * 1024 * 1024 or total > 95 * 1024 * 1024:
            raise ValueError("max_update_bytes exceeded")
        with Path(item["name"]).open("xb") as destination:
            for part, offset in enumerate(range(0, item["size"], 16 * 1024 * 1024)):
                chunk = Path(f"update-{index}-{part}.bin")
                expected_size = min(16 * 1024 * 1024, item["size"] - offset)
                if chunk.stat().st_size != expected_size:
                    raise ValueError("update chunk size differs")
                with chunk.open("rb") as source:
                    while data := source.read(1024 * 1024):
                        destination.write(data)
                chunk.unlink()
        if digest(Path(item["name"])) != item["sha256"]:
            raise ValueError("update identity differs")
    result: dict[str, Any] = {}
    for name in DATABASES:
        target = manifest["targets"][name]
        current = SEED / f"{name}.cvd"
        replacement = Path(f"{name}.cvd")
        if replacement.exists():
            current = replacement.resolve()
        checked(["sigtool", "--info", str(current)], deadline)
        expanded = Path("databases") / name
        expanded.mkdir(parents=True)
        checked(["sigtool", f"--unpack={current}"], deadline, cwd=expanded)
        # CVD authentication covers the archive payload; use its embedded metadata.
        metadata = header(expanded / f"{name}.info")
        if replacement.exists():
            if metadata["version"] != target:
                raise ValueError("replacement version differs")
        elif target != metadata["version"]:
            if not metadata["version"] < target <= metadata["version"] + 90:
                raise ValueError("invalid patch chain")
            for version in range(metadata["version"] + 1, target + 1):
                patch = Path(f"{name}-{version}.cdiff").resolve()
                checked(["sigtool", f"--run-cdiff={patch}"], deadline, cwd=expanded)
                metadata = header(expanded / f"{name}.info")
                if metadata["version"] != version:
                    raise ValueError("patch target version differs")
            current = expanded.resolve()
        if metadata["flevel"] > flevel:
            raise Refused("database requires a newer engine")
        if metadata["built"] > request["now"] + 300:
            raise Refused("database timestamp is in the future")
        result[name] = metadata | {"path": str(current)}
    if request["now"] - result["daily"]["built"] > request["max_age_seconds"]:
        raise Refused("max_database_age_days exceeded")
    Path("prepared.json").write_text(json.dumps(result), encoding="utf-8")
    return {name: {k: v for k, v in data.items() if k != "path"} for name, data in result.items()}


def zip_preflight(data: bytes, request: dict[str, Any], deadline: float) -> None:
    """Bound actual ZIP expansion before the engine's format-specific skip paths."""
    consumed, count = len(data), 0

    def visit(content: bytes, depth: int) -> None:
        nonlocal consumed, count
        source = io.BytesIO(content)
        if not zipfile.is_zipfile(source):
            return
        if depth > request["max_recursion"]:
            raise Refused("max_recursion exceeded")
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                if time.monotonic() >= deadline:
                    raise TimeoutError("ZIP preflight timed out")
                count += 1
                if count > request["max_files"]:
                    raise Refused("max_files exceeded")
                if item.flag_bits & 1:
                    raise Refused("encrypted ZIP member")
                if item.file_size > request["max_filesize"]:
                    raise Refused("max_filesize exceeded")
                if consumed + item.file_size > request["max_scansize"]:
                    raise Refused("max_scansize exceeded")
                extracted = bytearray()
                with archive.open(item) as member:
                    while chunk := member.read(64 * 1024):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("ZIP preflight timed out")
                        consumed += len(chunk)
                        extracted.extend(chunk)
                        if len(extracted) > request["max_filesize"]:
                            raise Refused("max_filesize exceeded")
                        if consumed > request["max_scansize"]:
                            raise Refused("max_scansize exceeded")
                visit(bytes(extracted), depth + 1)

    visit(data, 1)


def scan(request: dict[str, Any], deadline: float) -> tuple[str, str]:
    """Classify native alerts without representing policy limits as malware detections."""
    databases = json.loads(Path("prepared.json").read_text("utf-8"))
    if Path("input.bin").stat().st_size > request["max_bytes"]:
        raise Refused("max_bytes exceeded")
    if Path("input.bin").stat().st_size > request["max_filesize"]:
        raise Refused("max_filesize exceeded")
    if Path("input.bin").stat().st_size > request["max_scansize"]:
        raise Refused("max_scansize exceeded")
    zip_preflight(Path("input.bin").read_bytes(), request, deadline)
    command = [
        "clamscan",
        "--no-summary",
        "--stdout",
        "--infected",
        "--allmatch",
        "--alert-exceeds-max=yes",
        "--alert-encrypted=yes",
        "--alert-broken=yes",
        f"--max-filesize={request['max_filesize']}",
        f"--max-scansize={request['max_scansize']}",
        f"--max-files={request['max_files']}",
        f"--max-recursion={request['max_recursion']}",
        f"--max-scantime={max(1, int(request['timeout'] * 1000))}",
    ]
    if Path(databases["bytecode"]["path"]).is_dir():
        # The signed CDIFF chain authenticates unpacked bytecode before this phase.
        command.append("--bytecode-unsigned=yes")
    for name in DATABASES:
        command.extend(["--database", databases[name]["path"]])
    code, output = run([*command, "input.bin"], deadline)
    if code not in (0, 1) or len(output) > MAX_DIAGNOSTIC:
        raise ValueError(output[:MAX_DIAGNOSTIC] or "engine did not complete")
    alerts = re.findall(r"^input\.bin: ([^\r\n]+) FOUND$", output, re.MULTILINE)
    if code == 0:
        if output.strip():
            raise ValueError(output)
        return "clean", ""
    if not alerts:
        raise ValueError("engine detection response is malformed")
    policy = ("Heuristics.Limits.Exceeded", "Heuristics.Encrypted", "Heuristics.Broken")
    if all(alert.startswith(policy) for alert in alerts):
        return "refused", "\n".join(alerts)
    return "infected", "\n".join(alerts)


def main() -> None:
    """Emit one bounded response; raw engine output is never a protocol message."""
    try:
        if sys.argv[1] == "seed":
            response = {"status": "seed", "databases": seed()}
        else:
            request = json.loads(Path("request.json").read_text("utf-8"))
            deadline = time.monotonic() + request["timeout"]
            if sys.argv[1] == "prepare":
                response = {"status": "prepared", "databases": prepare(request, deadline)}
            elif sys.argv[1] == "scan":
                verdict, diagnostic = scan(request, deadline)
                response = {"status": verdict, "diagnostic": diagnostic}
            else:
                raise ValueError("unknown operation")
    except Refused as exc:
        response = {"status": "refused", "diagnostic": str(exc)[:MAX_DIAGNOSTIC]}
    except Exception as exc:
        response = {"status": "error", "diagnostic": str(exc)[:MAX_DIAGNOSTIC]}
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    main()
