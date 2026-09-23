"""Docker's engine-side subject for the shared confinement probe.

The observer image is trusted host tooling. Its Python reads kernel-provided process and
mount namespaces; no executable or library from the workload is used for observation.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tarfile
import uuid
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, cast

from maf_sandbox import Sandbox
from maf_sandbox.conformance import SandboxFingerprint

from ._backend import (
    _PROXY_CA_PATH,  # pyright: ignore[reportPrivateUsage]
    _DockerSandbox,  # pyright: ignore[reportPrivateUsage]
    _proxy_name,  # pyright: ignore[reportPrivateUsage]
)

_OUTPUT_LIMIT = 4 * 1024 * 1024


def _network_file_roots(inspected: dict[str, Any]) -> dict[str, str]:
    roots: dict[str, str] = {}
    for path, field, name in (
        ("/etc/hostname", "HostnamePath", "hostname"),
        ("/etc/hosts", "HostsPath", "hosts"),
        ("/etc/resolv.conf", "ResolvConfPath", "resolv.conf"),
    ):
        suffix = f"/{inspected['Id']}/{name}"
        source = inspected.get(field)
        if (
            isinstance(source, str)
            and source.startswith("/")
            and source.endswith(suffix)
            and not any(
                path == mount["Destination"]
                or path.startswith(mount["Destination"].rstrip("/") + "/")
                for mount in inspected["Mounts"]
            )
        ):
            # Mount roots are relative to their filesystem, not the daemon's root.
            roots[path] = suffix
    return roots


def _changed_paths(output: bytes) -> set[str]:
    changed: set[str] = set()
    for line in output.decode("utf-8").splitlines():
        if len(line) < 3 or line[:2] not in ("A ", "C ", "D ") or line[2] != "/":
            raise RuntimeError("Docker returned an invalid filesystem diff")
        changed.add(line[2:])
    return changed


class DockerFingerprintSubject:
    """Measure a quiescent Linux sandbox with a separate, host-trusted Python 3.12+ image.

    The image must be available locally. It is pinned to its engine image ID on first use.
    Non-Linux engines are unsupported; unreadable storage, shared PID namespaces, writable
    declared mounts, and exceeded limits fail the probe. Backend-provisioned CAs must match
    the current trusted proxy; their ancestors retain mode and ownership checks. Create a
    new subject per probe.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        observer_image: str,
        timeout: float = 60,
        max_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 10000,
    ) -> None:
        if not isinstance(sandbox, _DockerSandbox):
            raise TypeError("DockerFingerprintSubject requires a Docker sandbox")
        if not observer_image or observer_image.startswith("-"):
            raise ValueError("observer_image must name a trusted local Python image")
        if timeout <= 0 or max_bytes <= 0 or max_entries <= 0:
            raise ValueError("observer limits must be positive")
        self._run = sandbox._run  # pyright: ignore[reportPrivateUsage]
        self._name = sandbox.container_name
        self._proxy_ca_path = sandbox._proxy_ca_path  # pyright: ignore[reportPrivateUsage]
        self._image = observer_image
        self._image_id: str | None = None
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._container_id: str | None = None
        self._baseline: dict[str, str] | None = None
        self._mounts: str | None = None

    async def _command(self, *args: str) -> bytes:
        result = await self._run(*args, timeout=self._timeout, read_limit=_OUTPUT_LIMIT + 1)
        if len(result.stdout) > _OUTPUT_LIMIT:
            raise RuntimeError("Docker fingerprint output limit exceeded")
        if result.returncode:
            raise RuntimeError(f"Docker fingerprint {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    async def _inspect(self, target: str) -> dict[str, Any]:
        answer = json.loads(await self._command("inspect", target))
        if not isinstance(answer, list) or len(cast(list[object], answer)) != 1:
            raise RuntimeError("Docker returned an invalid container inspection")
        item = cast(list[object], answer)[0]
        if not isinstance(item, dict):
            raise RuntimeError("Docker returned an invalid container inspection")
        return cast(dict[str, Any], item)

    async def _provisioned_files(self) -> dict[str, str]:
        if self._proxy_ca_path is None:
            return {}
        archive = await self._command("cp", f"{_proxy_name(self._name)}:{_PROXY_CA_PATH}", "-")
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            members = bundle.getmembers()
            if len(members) != 1 or not members[0].isreg() or members[0].size > 8192:
                raise RuntimeError("proxy CA archive must contain one bounded regular file")
            stream = bundle.extractfile(members[0])
            if stream is None:
                raise RuntimeError("proxy CA archive is unreadable")
            certificate = stream.read(8193)
        if not certificate.startswith(b"-----BEGIN CERTIFICATE-----"):
            raise RuntimeError("proxy CA archive does not contain a certificate")
        return {self._proxy_ca_path: hashlib.sha256(certificate).hexdigest()}

    async def fingerprint(self) -> SandboxFingerprint | None:
        """Combine rootfs diff with mounted-file contents and kernel process birth identities."""
        engine = (await self._command("version", "--format", "{{.Server.Os}}")).strip()
        if engine != b"linux":
            if engine in (b"windows", b"freebsd") and self._baseline is None:
                return None
            raise RuntimeError("Docker fingerprint requires an established Linux engine")
        inspected = await self._inspect(self._name)
        container = inspected["Id"]
        if not isinstance(container, str) or not re.fullmatch(r"[0-9a-f]{64}", container):
            raise RuntimeError("Docker returned an invalid container ID")
        if self._container_id is not None and container != self._container_id:
            raise RuntimeError("fingerprint container was replaced")
        if inspected["State"]["Running"] is not True:
            raise RuntimeError("fingerprint container is not running")
        host = inspected["HostConfig"]
        if host["PidMode"] not in ("", "private") or host.get("Privileged") is not False:
            raise RuntimeError("fingerprint requires a private, unprivileged PID namespace")
        for mount in inspected["Mounts"]:
            if mount.get("RW") is not False:
                raise RuntimeError(
                    f"fingerprint refuses writable mount: {mount.get('Destination')}"
                )
        self._container_id = container
        changed = _changed_paths(await self._command("diff", container))
        provisioned = await self._provisioned_files()
        provisioned_paths = set(provisioned)
        for path in provisioned:
            provisioned_paths.update(
                str(parent) for parent in PurePosixPath(path).parents if str(parent) != "/"
            )
        # Provisioned files and their ancestors are also measured by the trusted observer.
        changed.difference_update(provisioned_paths)
        if self._baseline is None and changed:
            raise RuntimeError(f"sandbox is not pristine: {sorted(changed)!r}")
        if self._image_id is None:
            image = json.loads(await self._command("image", "inspect", self._image))
            self._image_id = image[0]["Id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", self._image_id or ""):
                raise RuntimeError("Docker returned an invalid observer image ID")
            if image[0]["Config"].get("Volumes"):
                raise RuntimeError("observer image must not declare volumes")
        assert self._image_id is not None
        observer = "maf-fingerprint-" + uuid.uuid4().hex
        script = files("maf_sandbox_docker").joinpath("_observer.py").read_text(encoding="utf-8")
        try:
            output = await self._command(
                "run",
                "--pull=never",
                "--name",
                observer,
                "--read-only",
                "--init=false",
                "--network=none",
                f"--pid=container:{container}",
                "--cap-drop=ALL",
                "--cap-add=SYS_PTRACE",
                "--security-opt=no-new-privileges",
                "--user=0:0",
                "--entrypoint=python",
                self._image_id,
                "-I",
                "-S",
                "-c",
                script,
                str(self._max_bytes),
                str(self._max_entries),
                json.dumps(_network_file_roots(inspected)),
                json.dumps(provisioned),
            )
        finally:
            # Killing the client on timeout/cancellation does not stop its container.
            await asyncio.shield(self._command("rm", "-f", observer))
        observed = json.loads(output)
        entries = observed["entries"]
        programs = observed["processes"]
        mounts = observed["mounts"]
        tmpfs_entries = observed["tmpfs_entries"]
        if (
            not isinstance(entries, dict)
            or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in cast(dict[object, object], entries).items()
            )
            or not isinstance(programs, list)
            or not all(
                isinstance(p, str) and re.fullmatch(r"\d+:\d+", p)
                for p in cast(list[object], programs)
            )
            or not isinstance(mounts, str)
            or not isinstance(tmpfs_entries, list)
            or not all(isinstance(p, str) for p in cast(list[object], tmpfs_entries))
        ):
            raise RuntimeError("observer returned an invalid fingerprint")
        entries = cast(dict[str, str], entries)
        if not provisioned_paths <= entries.keys():
            raise RuntimeError("observer omitted a provisioned path")
        programs = cast(list[str], programs)
        if self._baseline is None:
            if tmpfs_entries:
                raise RuntimeError(f"sandbox tmpfs is not pristine: {tmpfs_entries!r}")
            self._baseline = dict(entries)
            self._mounts = mounts
        else:
            if mounts != self._mounts:
                raise RuntimeError("workload mount inventory changed")
            changed.update(
                path
                for path in entries.keys() | self._baseline.keys()
                if entries.get(path) != self._baseline.get(path)
            )
        after = await self._inspect(self._name)
        if (
            after["Id"] != container
            or after["State"]["Running"] is not True
            or after["State"]["StartedAt"] != inspected["State"]["StartedAt"]
        ):
            raise RuntimeError("container changed during observation")
        changed.update(_changed_paths(await self._command("diff", container)) - provisioned_paths)
        return SandboxFingerprint(frozenset(changed), frozenset(programs))
