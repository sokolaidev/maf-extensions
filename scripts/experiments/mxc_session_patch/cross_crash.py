"""Kill a disposable call worker at one explicit native execution boundary."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import cross_active, remote_host
from . import process_identity as identity
from . import remote_api as api
from .remote_authority import Authority

CODE = b"assert remote_counter == 41; remote_counter = 99; print(remote_counter)"
WAIT_SECONDS = 450
READY = {"boundary": "before_remote_publication", "native_counter": 99}


def command() -> list[str]:
    """Launch the isolated call worker with configuration on stdin."""
    # A venv launcher can outlive or detach its Python payload on Windows.
    return [
        getattr(sys, "_base_executable", sys.executable),
        "-c",
        (
            f"import runpy, sys; sys.path[:] = {[str(Path(__file__).resolve().parents[3]), *sys.path]!r}; "
            "runpy.run_module('scripts.experiments.mxc_session_patch.cross_crash', run_name='__main__')"
        ),
    ]


def interrupt(
    host: remote_host.Host,
    client: api.Client,
    root: Path,
    bundle: Path,
    *,
    active_guest: bool = False,
) -> dict:
    """Require the selected native boundary, then kill and reap its worker process."""
    if host.owner is None:
        raise api.Refused("crash worker requires an owned generation")
    ready = root / "worker-crash-ready.json"
    if ready.exists():
        raise api.Refused("crash worker readiness already exists")
    if active_guest and list((host.root / "scratch").glob("*")):
        raise api.Refused("active crash requires reconciled scratch")
    active = None
    config = {
        "active_guest": active_guest,
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
            while True:
                if active_guest:
                    active = cross_active.observe(host.root)
                    if active is not None:
                        if identity.stopped(active[0]):
                            raise api.Refused("active helper exited before worker termination")
                        if active[2]:
                            break
                elif ready.exists():
                    break
                if worker.poll() is not None or time.monotonic() >= deadline:
                    raise api.Refused("crash worker did not reach the selected native boundary")
                time.sleep(0.02)
            if not active_guest and (
                ready.stat().st_size > 1024 or json.loads(ready.read_bytes()) != READY
            ):
                raise api.Refused("crash worker readiness differs")
            if worker.poll() is not None:
                raise api.Refused("crash worker exited before forced termination")
            stopped_by = time.monotonic() + cross_active.STOP_SECONDS
            worker.kill()
            worker.wait(timeout=15)
            if worker.returncode == 0:
                raise api.Refused("crash worker exited successfully")
            if active_guest:
                assert active is not None
                while True:
                    stopped = identity.stopped(active[0])
                    if time.monotonic() >= stopped_by:
                        raise api.Refused("native helper survived owner loss deadline")
                    if stopped:
                        break
                    time.sleep(0.02)
                if (active[1] / "candidate").exists() or (active[1] / "native.json").exists():
                    raise api.Refused("active helper produced completion after owner loss")
                return dict(cross_active.RESULT)
            return {**READY, "worker_killed": True}
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=15)
            if active is not None:
                identity.terminate(active[0])


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
    """Execute one fixed crash probe in a disposable call worker."""
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
    active_guest = config.get("active_guest", False)
    if active_guest:
        remote_host.execute = cross_active.execute
    host.call(
        client,
        config["owner"],
        "active-crash" if active_guest else "crash",
        cross_active.CODE if active_guest else CODE,
        bundle / "helper",
        bundle / "startup",
        1024,
    )
    raise api.Refused("crash worker returned without forced termination")


if __name__ == "__main__":
    raise SystemExit(main())
