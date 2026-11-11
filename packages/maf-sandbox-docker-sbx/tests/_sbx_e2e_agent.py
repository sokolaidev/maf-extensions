"""Standard-library guest probe for signing through the forwarded SSH agent."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import struct
import sys
import textwrap


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read(connection: socket.socket, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        part = connection.recv(size - len(value))
        if not part:
            raise ValueError("agent closed an incomplete response")
        value.extend(part)
    return bytes(value)


def sign(guest_socket: str, key: bytes, challenge: bytes) -> str:
    """Request a Git-namespace SSHSIG using only the fixture's public key."""
    fields = _string(b"git") + _string(b"") + _string(b"sha512")
    data = b"SSHSIG" + fields + _string(hashlib.sha512(challenge).digest())
    request = b"\x0d" + _string(key) + _string(data) + struct.pack(">I", 0)
    with socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(guest_socket)
        connection.sendall(_string(request))
        size = struct.unpack(">I", _read(connection, 4))[0]
        if not 5 <= size <= 1024 * 1024:
            raise ValueError(f"agent refused signing or returned an invalid frame size: {size}")
        response = _read(connection, size)
    if response[0] != 14 or struct.unpack(">I", response[1:5])[0] != len(response) - 5:
        raise ValueError("agent did not return a complete signature")
    blob = b"SSHSIG" + struct.pack(">I", 1) + _string(key) + fields + response[1:]
    encoded = "\n".join(textwrap.wrap(base64.b64encode(blob).decode(), 76))
    return f"-----BEGIN SSH SIGNATURE-----\n{encoded}\n-----END SSH SIGNATURE-----\n"


def main() -> None:
    """Report socket state even when the agent cannot sign the challenge."""
    guest_socket, encoded_key, challenge = sys.argv[1:]
    report: dict[str, object] = {
        "socket": guest_socket,
        "ssh_auth_sock": os.environ.get("SSH_AUTH_SOCK"),
        "socket_present": os.path.lexists(guest_socket),
        "socket_is_socket": False,
    }
    try:
        report["socket_is_socket"] = stat.S_ISSOCK(os.stat(guest_socket).st_mode)
        assert report["socket_is_socket"], "forwarded path is not a Unix socket"
        report["signature"] = sign(guest_socket, base64.b64decode(encoded_key), challenge.encode())
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
