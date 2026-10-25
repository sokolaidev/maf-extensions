"""Kill a disposable native worker after capture and before coordinator publication."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import remote_api as api
from . import remote_host
from .remote_authority import Authority

CODE = b"assert remote_counter == 41; remote_counter = 99; print(remote_counter)"
WAIT_SECONDS = 450
READY = {"boundary": "before_remote_publication", "native_counter": 99}


def command() -> list[str]:
    """Launch the isolated call worker with configuration on stdin."""
    return [sys.executable, "-m", "scripts.experiments.mxc_session_patch.cross_crash"]


def interrupt(host: remote_host.Host, client: api.Client, root: Path, bundle: Path) -> dict:
    """Require a captured native candidate, then kill and reap its worker process."""
    if host.owner is None:
        raise api.Refused("crash worker requires an owned generation")
    ready = root / "worker-crash-ready.json"
    if ready.exists():
        raise api.Refused("crash worker readiness already exists")
    config = {
        "root": str(root),
        "host_root": str(host.root),
        "bundle": str(bundle),
        "owner": Authority._encode(host.owner),
        "profile": dict(host.config.profile),
        "limits": asdict(host.config.limits),
        "scratch": asdict(host.scratch),
        "storage": asdict(host.storage),
        "address": client.address,
    }
    with subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL) as worker:
        try:
            assert worker.stdin is not None
            worker.stdin.write(json.dumps(config).encode())
            worker.stdin.close()
            deadline = time.monotonic() + WAIT_SECONDS
            while not ready.exists():
                if worker.poll() is not None or time.monotonic() >= deadline:
                    raise api.Refused("crash worker did not reach native publication")
                time.sleep(0.02)
            if ready.stat().st_size > 1024 or json.loads(ready.read_bytes()) != READY:
                raise api.Refused("crash worker readiness differs")
            if worker.poll() is not None:
                raise api.Refused("crash worker exited before forced termination")
            worker.kill()
            worker.wait(timeout=15)
            if worker.returncode == 0:
                raise api.Refused("crash worker exited successfully")
            return {**READY, "worker_killed": True}
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=15)


class PausedClient(api.Client):
    """Perform real admission and restore, but never send the candidate publication."""

    ready: Path

    def publish(
        self, session: str, owner: str, call: str, request: bytes, checkpoint: Path, result: bytes
    ) -> None:
        if call != "crash" or request != CODE or not checkpoint.is_dir():
            raise api.Refused("unexpected crash worker publication")
        output = api._bytes(json.loads(result)["console_base64"], 1024).strip()
        if output != b"99":
            raise api.Refused("crash worker native state differs")
        temporary = self.ready.with_suffix(".tmp")
        temporary.write_text(json.dumps(READY), encoding="utf-8")
        temporary.replace(self.ready)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(1)
        raise api.Refused("crash worker was not terminated at the publication boundary")


def main() -> int:
    """Execute only the fixed unpublished-state control in a disposable child process."""
    config = json.load(sys.stdin)
    root, bundle = Path(config["root"]), Path(config["bundle"])
    owner = Authority._decode_owner(config["owner"])
    host = remote_host.Host(
        Path(config["host_root"]),
        owner.resource.host,
        owner.resource.boot,
        owner.session,
        api.Session(config["profile"], api.Limits(**config["limits"])),
        api.remote_scratch.Policy(**config["scratch"]),
        remote_host.physical.Policy(**config["storage"]),
        owner=owner,
    )
    client = PausedClient(
        tuple(config["address"]),
        api.client_context(root / "certificate.pem", root / "key.pem", root / "coordinator.pem"),
        "mxc-coordinator",
    )
    client.ready = root / "worker-crash-ready.json"
    host.call(client, config["owner"], "crash", CODE, bundle / "helper", bundle / "startup", 1024)
    raise api.Refused("crash worker returned without forced termination")


if __name__ == "__main__":
    raise SystemExit(main())
