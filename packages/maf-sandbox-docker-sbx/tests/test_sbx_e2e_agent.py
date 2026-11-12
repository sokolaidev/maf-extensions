"""The forwarding fixture requires a real signature, not merely a socket path."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import _sbx_e2e_agent as probe
import pytest
import test_sbx_e2e_host as host


@pytest.fixture
def agent_key(tmp_path):
    if shutil.which("ssh-keygen") is None:
        pytest.skip("OpenSSH is required for signature verification")
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


@pytest.mark.parametrize("fault", [None, "missing", "not-socket", "wrong-key", "wrong-challenge"])
def test_host_requires_a_signature_from_the_expected_agent(
    monkeypatch, tmp_path, agent_key, fault, capsys
):
    public = Path(f"{agent_key}.pub")
    monkeypatch.setenv("MAF_SANDBOX_SBX_E2E_AGENT_PUBLIC_KEY", str(public))
    monkeypatch.setattr(host, "_checked", lambda *args: b"[]" if "--json" in args else b"true")

    class SigningBackend:
        async def _sbx(self, *args, **kwargs):
            assert args[:4] == ("exec", "probe", "python3", "-c")
            assert args[5] == "/run/ssh-agent.sock"
            challenge = args[-1].encode()
            if fault == "wrong-challenge":
                challenge = b"different challenge"
            key = agent_key
            if fault == "wrong-key":
                key = tmp_path / "other-key"
                subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
                )
            signed = subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "git"],
                input=challenge,
                capture_output=True,
                check=True,
            )
            report = {
                "socket_present": fault != "missing",
                "socket_is_socket": fault != "not-socket",
                "signature": signed.stdout.decode().replace("\r\n", "\n"),
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(report).encode(), b"")

    if fault:
        with pytest.raises(AssertionError):
            asyncio.run(host._prove_forwarded_agent(SigningBackend(), "probe", tmp_path))
    else:
        asyncio.run(host._prove_forwarded_agent(SigningBackend(), "probe", tmp_path))
        assert "forwarded_agent_signature_verified" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="OpenSSH's Unix agent is required")
def test_guest_probe_signs_through_a_real_agent(tmp_path, agent_key):
    if shutil.which("ssh-agent") is None or shutil.which("ssh-add") is None:
        pytest.skip("OpenSSH agent tools are required")
    guest_socket = str(tmp_path / "agent.sock")
    with subprocess.Popen(
        ["ssh-agent", "-D", "-a", guest_socket], stdout=subprocess.DEVNULL
    ) as agent:
        try:
            deadline = time.monotonic() + 5
            while not Path(guest_socket).exists() and time.monotonic() < deadline:
                assert agent.poll() is None
                time.sleep(0.01)
            env = dict(os.environ, SSH_AUTH_SOCK=guest_socket)
            subprocess.run(["ssh-add", str(agent_key)], env=env, check=True, capture_output=True)
            public = Path(f"{agent_key}.pub").read_text().split()
            signature = probe.sign(guest_socket, base64.b64decode(public[1]), b"challenge")
            signed = tmp_path / "signed"
            signed.write_text(signature)
            allowed = tmp_path / "allowed"
            allowed.write_text(f"fixture {' '.join(public[:2])}\n")
            verified = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed),
                    "-I",
                    "fixture",
                    "-n",
                    "git",
                    "-s",
                    str(signed),
                ],
                input=b"challenge",
                capture_output=True,
            )
            assert verified.returncode == 0, (verified.stdout, verified.stderr)
        finally:
            agent.terminate()
            agent.wait(timeout=5)


def test_guest_probe_reports_a_missing_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["probe", str(tmp_path / "missing"), "a2V5", "challenge"])
    with pytest.raises(FileNotFoundError):
        probe.main()
    report = json.loads(capsys.readouterr().out)
    assert report["socket_present"] is False
    assert report["socket_is_socket"] is False
    assert "FileNotFoundError" in report["error"]
