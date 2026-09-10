"""Bounded Linux proc reader, executed with -I -S inside the sandbox."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MAX_PROCESSES = 256
MAX_BYTES = 1024 * 1024
MAX_FIELD = 2048


def snapshot() -> dict[str, Any]:
    """Read available metadata without following guest paths or collecting the environment."""
    deadline = time.monotonic() + 2
    processes: list[dict[str, Any]] = []
    used = 0
    incomplete = False
    names: list[int] = []
    with os.scandir("/proc") as entries:
        for entry in entries:
            if len(names) >= MAX_PROCESSES * 4 or time.monotonic() >= deadline:
                incomplete = True
                break
            if entry.name.isascii() and entry.name.isdigit():
                names.append(int(entry.name))
    names.sort()
    for pid in names:
        if pid == os.getpid():
            continue
        if len(processes) >= MAX_PROCESSES or time.monotonic() >= deadline:
            incomplete = True
            break
        root = Path("/proc") / str(pid)
        unavailable: list[str] = []
        truncated = False

        def read(name: str, limit: int) -> bytes | None:
            nonlocal truncated
            try:
                with (root / name).open("rb") as stream:
                    value = stream.read(limit + 1)
                truncated |= len(value) > limit
                return value[:limit]
            except OSError:
                unavailable.append(name)
                return None

        def link(name: str) -> str | None:
            nonlocal truncated
            try:
                value = os.readlink(root / name)
                truncated |= len(value) > MAX_FIELD
                return value[:MAX_FIELD]
            except OSError:
                unavailable.append(name)
                return None

        raw = read("stat", 8192)
        if raw is None:
            incomplete = True
            continue
        try:
            stat = raw.decode("utf-8", "replace")
            fields = stat.rsplit(")", 1)[1].split()
            record: dict[str, Any] = {
                "pid": pid,
                "ppid": int(fields[1]),
                "pgid": int(fields[2]),
                "sid": int(fields[3]),
                "state": fields[0],
                "start_ticks": int(fields[19]),
                "name": stat.split("(", 1)[1].rsplit(")", 1)[0][:MAX_FIELD],
                "threads": int(fields[17]),
                "user_ticks": int(fields[11]),
                "system_ticks": int(fields[12]),
                "virtual_bytes": int(fields[20]),
                "rss_bytes": int(fields[21]) * getattr(os, "sysconf")("SC_PAGE_SIZE"),
            }
        except (ValueError, IndexError):
            incomplete = True
            continue
        status = read("status", 65536)
        if status is not None:
            rows = dict(
                line.split(":", 1)
                for line in status.decode("utf-8", "replace").splitlines()
                if ":" in line
            )
            for source, real, effective in (
                ("Uid", "uid", "effective_uid"),
                ("Gid", "gid", "effective_gid"),
            ):
                values = rows.get(source, "").split()
                if len(values) >= 2:
                    record[real], record[effective] = int(values[0]), int(values[1])
                else:
                    unavailable.append(source)
            groups = rows.get("Groups", "").split()
            truncated |= len(groups) > 256
            record["groups"] = [int(g) for g in groups[:256]]
        else:
            unavailable.extend(("uid", "effective_uid", "gid", "effective_gid", "groups"))
        try:
            import pwd

            record["username"] = getattr(pwd, "getpwuid")(record["uid"]).pw_name[:MAX_FIELD]
        except (ImportError, KeyError):
            unavailable.append("username")
        argv = read("cmdline", 8192)
        if argv is not None:
            args = argv.rstrip(b"\0").split(b"\0") if argv else []
            truncated |= len(args) > 64 or any(len(a) > MAX_FIELD for a in args)
            record["argv"] = [a[:MAX_FIELD].decode("utf-8", "replace") for a in args[:64]]
            record["command"] = " ".join(record["argv"])[:8192]
        record["executable"], record["cwd"] = link("exe"), link("cwd")
        # Metadata can straddle an exit and PID reuse; discard a mixed record.
        again = read("stat", 8192)
        if again is None or again.rsplit(b")", 1)[1].split()[19] != fields[19].encode():
            incomplete = True
            continue
        record["unavailable"], record["truncated"] = unavailable, truncated
        size = len(json.dumps(record, ensure_ascii=True))
        if used + size > MAX_BYTES - 1024:
            incomplete = True
            break
        used += size
        processes.append(record)
    return {"processes": processes, "incomplete": incomplete}


def signal_processes(targets: list[list[int]]) -> list[dict[str, Any]]:
    """Check retained start ticks immediately before signalling each observed descendant."""
    outcomes: list[dict[str, Any]] = []
    for pid, start in targets[:MAX_PROCESSES]:
        outcome = "refused"
        signal = None
        if pid > 1:
            try:
                stat = (Path("/proc") / str(pid) / "stat").read_bytes()
                if int(stat.rsplit(b")", 1)[1].split()[19]) == start:
                    signal = "SIGKILL"
                    os.kill(pid, 9)
                    outcome = "sent"
                else:
                    outcome = "replaced"
            except (FileNotFoundError, ProcessLookupError):
                outcome = "absent"
            except (OSError, ValueError, IndexError):
                outcome = "refused"
        outcomes.append({"pid": pid, "start_ticks": start, "outcome": outcome, "signal": signal})
    return outcomes


if __name__ == "__main__":
    answer = (
        signal_processes(json.loads(sys.argv[2]))
        if len(sys.argv) == 3 and sys.argv[1] == "--signal"
        else snapshot()
    )
    print(json.dumps(answer, ensure_ascii=True))
