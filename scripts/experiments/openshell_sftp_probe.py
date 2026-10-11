# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0", "paramiko==4.0.0"]
# ///
"""Exercise official native SFTP through the pinned gateway's authenticated SSH relay."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import stat
import threading
from contextlib import contextmanager
from importlib import import_module

from openshell_files_probe import execute, main


@contextmanager
def native_sftp(client, name, sandbox_id):
    """Bridge the public ForwardTcp stream to a bounded-lifetime local SSH client."""
    paramiko = import_module("paramiko")
    session = client.rpc(
        "CreateSshSession",
        client.pb.CreateSshSessionRequest(sandbox=name, workspace_scope=client.scope),
    )
    if session.sandbox_id != sandbox_id:
        raise RuntimeError("SSH session targets another physical sandbox")
    local, bridge = socket.socketpair()
    bridge.settimeout(10)
    stopped = threading.Event()
    errors = []

    def requests():
        yield client.pb.TcpForwardFrame(
            init=client.pb.TcpForwardInit(
                sandbox=name,
                workspace=client.scope.workspace,
                service_id="spike-sftp",
                ssh=client.pb.SshRelayTarget(),
                authorization_token=session.token,
            )
        )
        while not stopped.is_set():
            try:
                data = bridge.recv(65536)
            except OSError:
                return
            if not data:
                return
            yield client.pb.TcpForwardFrame(data=data)

    call = client.stub.ForwardTcp(
        requests(), metadata=[("authorization", "Bearer " + client.token)], timeout=60
    )

    def receive():
        try:
            for frame in call:
                if frame.WhichOneof("payload") != "data":
                    raise RuntimeError("unexpected relay frame")
                bridge.sendall(frame.data)
        except Exception as exc:
            if not stopped.is_set():
                errors.append(type(exc).__name__)
        finally:
            try:
                bridge.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    transport = paramiko.Transport(local)
    sftp = None
    try:
        transport.start_client(timeout=10)
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()
        ).decode().rstrip("=")
        if session.host_key_fingerprint and session.host_key_fingerprint != fingerprint:
            raise RuntimeError("SSH host key mismatch")
        transport.auth_none("sandbox")
        sftp = transport.open_sftp_client()
        sftp.get_channel().settimeout(5)
        yield sftp
        if errors:
            raise RuntimeError("relay failed: " + ",".join(errors))
    finally:
        stopped.set()
        if sftp is not None:
            sftp.close()
        transport.close()
        call.cancel()
        bridge.close()
        local.close()
        receiver.join(timeout=5)
        if receiver.is_alive():
            raise RuntimeError("relay observer failed to stop")


def measure_sftp(client, name, sandbox_id):
    """Compare SFTP operations with the guest filesystem without declaring suite conformance."""
    results = []

    def emit(case, passed, **observed):
        row = {"sftp_case": case, "passed": bool(passed), **observed}
        results.append(row)
        print(json.dumps(row), flush=True)
        if not passed:
            raise RuntimeError("unexpected native SFTP observation: " + case)

    def refused(sftp, path):
        try:
            with sftp.open(path, "rb") as stream:
                stream.read(1)
        except OSError as exc:
            return exc.errno
        return None

    payload = bytes(range(256)) * 512
    with native_sftp(client, name, sandbox_id) as sftp:
        root = sftp.normalize(".")
        names = sftp.listdir(".")
        print(json.dumps({"sftp_root": root, "initial_names": names}), flush=True)
        emit("root_maps_to_guest_workdir", root == "/" and "nested" in names)
        with sftp.open("native.bin", "wb") as stream:
            stream.write(payload)
        with sftp.open("native.bin", "rb") as stream:
            actual = stream.read(len(payload) + 1)
        emit(
            "binary_roundtrip",
            actual == payload,
            bytes=len(actual),
            sha256=hashlib.sha256(actual).hexdigest(),
        )
        status, output, error = execute(
            client,
            name,
            "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/sandbox/native.bin').read_bytes()).hexdigest())",
        )
        emit(
            "guest_sees_native_bytes",
            status == 0
            and not error
            and output.strip().decode() == hashlib.sha256(payload).hexdigest(),
        )
        meta = sftp.lstat("native.bin")
        emit(
            "regular_metadata",
            stat.S_ISREG(meta.st_mode) and meta.st_size == len(payload),
            uid=meta.st_uid,
            size=meta.st_size,
        )
        for path in ("/native.bin", "/sandbox/native.bin"):
            with sftp.open(path, "rb") as stream:
                emit("absolute_alias_" + path.replace("/", "_"), stream.read(4) == payload[:4])
        for label, path, expected in (
            ("parent_refused", "../tmp/spike1797-value", 13),
            ("outside_absolute_is_virtual", "/tmp/spike1797-value", 2),
            ("outside_link_does_not_escape", "linked-file", 2),
        ):
            error_number = refused(sftp, path)
            emit(label, error_number == expected, errno=error_number)
        try:
            sftp.symlink("native.bin", "native-link")
        except OSError as exc:
            emit("paramiko_symlink_existing_target_refused", str(exc) == "Failure", error=str(exc))
        else:
            emit("paramiko_symlink_existing_target_refused", False)
        sftp.symlink("wire-target", "wire-link")
        emit(
            "paramiko_symlink_arguments_reversed",
            stat.S_ISLNK(sftp.lstat("wire-target").st_mode)
            and sftp.readlink("wire-target") == "wire-link",
        )
        status, output, error = execute(
            client,
            name,
            "from pathlib import Path; Path('/sandbox/native-link').symlink_to('native.bin'); Path('/sandbox/native-dir-link').symlink_to('nested',target_is_directory=True)",
        )
        if status != 0 or output or error:
            raise RuntimeError("guest link fixture failed")
        emit("lstat_reports_link", stat.S_ISLNK(sftp.lstat("native-link").st_mode))
        with sftp.open("native-link", "rb") as stream:
            emit("within_root_link_is_followed", stream.read(4) == payload[:4])
        with sftp.open("native-dir-link/input.bin", "rb") as stream:
            emit("within_root_parent_link_is_followed", stream.read(16) == b"guest")
        listing = {entry.filename: entry for entry in sftp.listdir_attr(".")}
        emit("listing_reports_link", stat.S_ISLNK(listing["native-link"].st_mode))
        with sftp.open("native.bin", "rb") as stream:
            prefix = stream.read(1025)
        emit(
            "bounded_read_can_detect_overflow",
            len(prefix) == 1025 and prefix == payload[:1025],
            requested=1025,
            application_limit=1024,
        )
        sftp.remove("native-link")
        emit("unlink_preserves_target", sftp.stat("native.bin").st_size == len(payload))
        status, output, error = execute(
            client,
            name,
            "from pathlib import Path; p=Path('/sandbox/native.bin'); p.write_bytes(b'guest-update'); print(p.read_bytes()==b'guest-update')",
        )
        with sftp.open("native.bin", "rb") as stream:
            updated = stream.read(32)
        emit(
            "guest_can_update_sftp_input",
            status == 0 and output == b"True\n" and not error and updated == b"guest-update",
        )
    return {
        "passed": len(results) == 18 and all(row["passed"] for row in results),
        "cases": len(results),
        "native_transfer": True,
        "suite_link_refusal": False,
        "concurrent_path_replacement_tested": False,
    }


if __name__ == "__main__":
    raise SystemExit(main(native_probe=measure_sftp))
