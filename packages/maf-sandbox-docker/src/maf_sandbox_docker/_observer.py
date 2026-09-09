"""Linux measurements run in the host-selected observer image, never in the workload."""

import hashlib
import json
import os
import stat
import sys


def _processes() -> list[str]:
    own = str(os.getpid())
    result: list[str] = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or name == own:
            continue
        with open(f"/proc/{name}/stat") as stream:
            fields = stream.read().rsplit(")", 1)[1].split()
        result.append(f"{name}:{fields[19]}")
    return sorted(result)


def _unescape(path: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        path = path.replace(encoded, decoded)
    return path


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    # Access time can change when the observer reads an entry.
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        getattr(info, "st_rdev"),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def measure(max_bytes: int, max_entries: int) -> dict[str, object]:
    """Read writable storage and process births through the workload's kernel namespace."""
    directory_flag: int = getattr(os, "O_DIRECTORY")
    nofollow_flag: int = getattr(os, "O_NOFOLLOW")
    nonblock_flag: int = getattr(os, "O_NONBLOCK")
    processes = _processes()
    with open("/proc/1/mountinfo") as stream:
        mountinfo = stream.read()
    mounts: dict[str, tuple[str, bool]] = {}
    for line in mountinfo.splitlines():
        fields = line.split()
        separator = fields.index("-")
        mounts[_unescape(fields[4])] = (fields[separator + 1], "rw" in fields[5].split(","))
    # These expose kernel state, outside the filesystem/process-residue contract.
    kernel = {"proc", "sysfs", "devpts", "mqueue", "cgroup", "cgroup2"}
    roots = sorted(
        path
        for path, (kind, writable) in mounts.items()
        if path != "/" and writable and kind not in kernel
    )
    entries: dict[str, str] = {}
    tmpfs_entries: list[str] = []
    remaining = max_bytes
    root = os.open("/proc/1/root", os.O_RDONLY | directory_flag)

    def open_parent(path: str) -> tuple[int, str]:
        parts = path.split("/")[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise RuntimeError("invalid mount path")
        parent = os.dup(root)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | directory_flag | nofollow_flag, dir_fd=parent)
                os.close(parent)
                parent = child
            return parent, parts[-1]
        except BaseException:
            os.close(parent)
            raise

    def record(parent: int, name: str, path: str, mount: str) -> None:
        nonlocal remaining
        if len(entries) >= max_entries:
            raise RuntimeError("observer entry limit exceeded")
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        metadata = _metadata(info)
        content = ""
        if stat.S_ISLNK(info.st_mode):
            content = os.readlink(name, dir_fd=parent)
        elif stat.S_ISREG(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | nofollow_flag | nonblock_flag, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if _metadata(os.fstat(stream.fileno())) != metadata:
                    raise RuntimeError("file changed during observation")
                digest = hashlib.sha256()
                while chunk := stream.read(min(65536, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise RuntimeError("observer byte limit exceeded")
                    digest.update(chunk)
                content = digest.hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | directory_flag | nofollow_flag, dir_fd=parent)
            try:
                if _metadata(os.fstat(descriptor)) != metadata:
                    raise RuntimeError("directory changed during observation")
                for child in sorted(os.listdir(descriptor)):
                    child_path = path + "/" + child
                    if child_path not in mounts:
                        record(descriptor, child, child_path, mount)
            finally:
                os.close(descriptor)
        if _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata:
            raise RuntimeError("entry changed during observation")
        entries[path] = json.dumps([metadata, content], separators=(",", ":"))
        if mounts[mount][0] == "tmpfs" and path != mount:
            if mount != "/dev" or path not in {
                "/dev/core",
                "/dev/fd",
                "/dev/full",
                "/dev/null",
                "/dev/ptmx",
                "/dev/random",
                "/dev/stderr",
                "/dev/stdin",
                "/dev/stdout",
                "/dev/tty",
                "/dev/urandom",
                "/dev/zero",
            }:
                tmpfs_entries.append(path)

    try:
        for path in roots:
            parent, name = open_parent(path)
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and mounts[path][0] != "tmpfs":
                    raise RuntimeError(f"unobservable writable mount: {path}")
                record(parent, name, path, path)
            finally:
                os.close(parent)
    finally:
        os.close(root)
    with open("/proc/1/mountinfo") as stream:
        if stream.read() != mountinfo or _processes() != processes:
            raise RuntimeError("namespace changed during observation")
    return {
        "entries": entries,
        "processes": processes,
        "mounts": mountinfo,
        "tmpfs_entries": sorted(tmpfs_entries),
    }


if __name__ == "__main__":
    print(json.dumps(measure(int(sys.argv[1]), int(sys.argv[2]))))
