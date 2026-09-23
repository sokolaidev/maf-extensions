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


def measure(
    max_bytes: int,
    max_entries: int,
    network_file_roots: dict[str, str],
    provisioned_files: dict[str, str] | None = None,
) -> dict[str, object]:
    """Read writable storage and process births through the workload's kernel namespace."""
    provisioned_files = provisioned_files or {}
    directory_flag: int = getattr(os, "O_DIRECTORY")
    nofollow_flag: int = getattr(os, "O_NOFOLLOW")
    nonblock_flag: int = getattr(os, "O_NONBLOCK")
    processes = _processes()
    with open("/proc/1/mountinfo") as stream:
        mountinfo = stream.read()
    mounts: dict[str, tuple[str, bool]] = {}
    network_files: set[str] = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        separator = fields.index("-")
        path = _unescape(fields[4])
        mounts[path] = (fields[separator + 1], "rw" in fields[5].split(","))
        if path in network_file_roots and _unescape(fields[3]).endswith(network_file_roots[path]):
            network_files.add(path)
        else:
            network_files.discard(path)
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

    def record(parent: int, name: str, path: str, mount: str | None) -> None:
        nonlocal remaining
        if len(entries) >= max_entries:
            raise RuntimeError("observer entry limit exceeded")
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        metadata = _metadata(info)
        if path in provisioned_files and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise RuntimeError(f"provisioned file is not a single regular file: {path}")
        if mount is None and path not in provisioned_files and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"provisioned file ancestor is not a directory: {path}")
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
        elif stat.S_ISDIR(info.st_mode) and mount is not None:
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
        attributes: dict[str, str] = {}
        if mount is None:
            descriptor = os.open(name, os.O_RDONLY | nofollow_flag | nonblock_flag, dir_fd=parent)
            try:
                if _metadata(os.fstat(descriptor)) != metadata:
                    raise RuntimeError("provisioned path changed during observation")
                names: list[str] = getattr(os, "listxattr")(descriptor)
                for attribute in sorted(names):
                    value: bytes = getattr(os, "getxattr")(descriptor, attribute)
                    remaining -= len(attribute.encode()) + len(value)
                    if remaining < 0:
                        raise RuntimeError("observer byte limit exceeded")
                    attributes[attribute] = hashlib.sha256(value).hexdigest()
            finally:
                os.close(descriptor)
        if _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata:
            raise RuntimeError("entry changed during observation")
        if path in provisioned_files:
            if content != provisioned_files[path]:
                raise RuntimeError(f"provisioned file differs from the trusted proxy: {path}")
            # A fresh proxy may rotate the CA between calls; mode and ownership must persist.
            metadata = (info.st_mode, info.st_nlink, info.st_uid, info.st_gid)
            content = "verified proxy CA"
        elif mount is None:
            # Adding and reclaiming call directories changes ancestor sizes and timestamps.
            metadata = metadata[:7]
        if path in network_files and stat.S_ISREG(info.st_mode):
            # Docker archive setup chowns these files even when ownership already matches.
            metadata = metadata[:-1]
        entries[path] = json.dumps([metadata, content, attributes], separators=(",", ":"))
        if mount is not None and mounts[mount][0] == "tmpfs" and path != mount:
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
        provisioned_paths = set(provisioned_files)
        for path in provisioned_files:
            ancestor = os.path.dirname(path)
            while ancestor not in ("", "/"):
                provisioned_paths.add(ancestor)
                ancestor = os.path.dirname(ancestor)
        for path in sorted(provisioned_paths):
            if any(
                path == mount or path.startswith(mount.rstrip("/") + "/")
                for mount in mounts
                if mount != "/"
            ):
                raise RuntimeError(f"provisioned path overlaps a mount: {path}")
            parent, name = open_parent(path)
            try:
                record(parent, name, path, None)
            finally:
                os.close(parent)
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
    print(
        json.dumps(
            measure(
                int(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3]), json.loads(sys.argv[4])
            )
        )
    )
