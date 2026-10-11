"""Qualify daemon-side removal denial on a disposable, mutually authenticated Docker engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from openclaw_docker_denial import outer_docker
from openclaw_gateway_lifecycle_check import qualify
from openclaw_http_observer import resolve_image


def run(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("Select a local parent context without a DOCKER_HOST override")
    current = json.loads(
        subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{json .}}"], text=True, encoding="utf-8"
        )
    )
    host = current["Endpoints"]["docker"]["Host"]
    image = resolve_image(args.image)
    daemon_image = resolve_image(args.daemon_image)
    parent_id = outer_docker(host, "info", "--format", "{{.ID}}")
    plugin = Path(__file__).with_name("openclaw_docker_authz") / "main.go"
    binary = root / "authorization-plugin"
    build_env = dict(os.environ, GOOS="linux", GOARCH="amd64", CGO_ENABLED="0")
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(binary), str(plugin)],
        env=build_env,
        check=True,
        timeout=120,
    )
    client = uuid.uuid4().hex
    name = "qualification-authz-" + client
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}
    daemon = None
    report = {}
    try:
        daemon = outer_docker(
            host,
            "create",
            "--name",
            name,
            "--label",
            "maf-qualification.authz=" + client,
            "--privileged",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "512",
            "--publish",
            "127.0.0.1::2376",
            "--env",
            "DOCKER_TLS_CERTDIR=/certs",
            "--env",
            "DOCKER_TLS_SAN=IP:127.0.0.1",
            "--entrypoint",
            "/bin/sh",
            daemon_image,
            "-c",
            "mkdir -p /control; /usr/local/bin/qualification-authz /control & exec dockerd-entrypoint.sh --authorization-plugin=qualification --feature=containerd-snapshotter=true --iptables=false --bridge=none",
            timeout=120,
        )
        outer_docker(host, "cp", str(binary), daemon + ":/usr/local/bin/qualification-authz")
        outer_docker(host, "start", daemon, timeout=45)
        inspected = json.loads(outer_docker(host, "inspect", daemon))[0]
        ports = inspected["NetworkSettings"]["Ports"]["2376/tcp"]
        if len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("Dedicated daemon was not published only on loopback")
        started = inspected["State"]["StartedAt"]
        deadline = time.monotonic() + 90
        while True:
            try:
                outer_docker(host, "exec", daemon, "test", "-s", "/certs/client/cert.pem")
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Dedicated daemon certificates not ready") from None
                time.sleep(0.25)
        outer_docker(host, "cp", daemon + ":/certs/client", str(root / "client"))
        (root / "docker-config").mkdir()
        for key in list(os.environ):
            if key.startswith("DOCKER_"):
                del os.environ[key]
        os.environ.update(
            DOCKER_HOST="tcp://127.0.0.1:" + ports[0]["HostPort"],
            DOCKER_TLS_VERIFY="1",
            DOCKER_CERT_PATH=str(root / "client"),
            DOCKER_CONFIG=str(root / "docker-config"),
        )
        while True:
            probe = subprocess.run(
                ["docker", "info", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            if probe.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Dedicated daemon did not become ready")
            time.sleep(0.25)
        info = json.loads(probe.stdout)
        if info["Plugins"]["Authorization"] != ["qualification"] or info["ID"] == parent_id:
            raise RuntimeError("Expected a distinct authorization-enabled daemon")
        archive = root / "prepared-image.tar"
        outer_docker(host, "image", "save", "-o", str(archive), image, timeout=180)
        subprocess.run(
            ["docker", "image", "load", "-i", str(archive)],
            capture_output=True,
            check=True,
            timeout=180,
        )
        if resolve_image(image) != image:
            raise RuntimeError("Dedicated daemon loaded a different prepared image")
        print(
            "Dedicated TLS daemon loaded the exact prepared image and authorization plugin",
            flush=True,
        )
        report = qualify(
            argparse.Namespace(
                root=root / "gateway",
                config=args.config,
                bicep_config=args.bicep_config,
                image=image,
                openclaw=args.openclaw,
                case="docker-denial",
                authz_outer_host=host,
                authz_daemon=daemon,
                authz_client=client,
            )
        )
        final_info = json.loads(
            subprocess.check_output(
                ["docker", "info", "--format", "{{json .}}"],
                text=True,
                encoding="utf-8",
                timeout=15,
            )
        )
        if final_info["ID"] != info["ID"] or final_info["Plugins"]["Authorization"] != [
            "qualification"
        ]:
            raise RuntimeError("Dedicated daemon identity or authorization changed")
        if json.loads(outer_docker(host, "inspect", daemon))[0]["State"]["StartedAt"] != started:
            raise RuntimeError("Dedicated daemon restarted during the case")
        if subprocess.check_output(
            ["docker", "ps", "-aq"], text=True, encoding="utf-8", timeout=15
        ).strip():
            raise RuntimeError("Dedicated daemon retained fixture containers")
        report.update(
            daemon_image=daemon_image,
            daemon_version=info["ServerVersion"],
            daemon_storage_driver=info["Driver"],
            daemon_tls=True,
            daemon_unchanged=True,
            plugin_source_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest(),
            plugin_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            go_version=subprocess.check_output(["go", "version"], text=True).strip(),
        )
    finally:
        for key in list(os.environ):
            if key.startswith("DOCKER_"):
                del os.environ[key]
        os.environ.update(saved_env)
        # A timed-out create can still have registered the exact named fixture.
        discovered = outer_docker(
            host, "ps", "-aq", "--filter", "label=maf-qualification.authz=" + client
        ).split()
        for owned in discovered:
            item = json.loads(outer_docker(host, "inspect", owned))[0]
            if (
                item["Name"] != "/" + name
                or item["Config"]["Labels"].get("maf-qualification.authz") != client
            ):
                raise RuntimeError("Unexpected container in dedicated daemon scope")
            volumes = {m["Name"] for m in item["Mounts"] if m["Type"] == "volume"}
            logs = subprocess.run(
                ["docker", "--host", host, "logs", owned], capture_output=True, timeout=15
            )
            (root / "daemon.log").write_bytes(logs.stdout + logs.stderr)
            outer_docker(host, "rm", "-fv", owned, timeout=45)
            if volumes & set(
                outer_docker(host, "volume", "ls", "--format", "{{.Name}}").splitlines()
            ):
                raise RuntimeError("Dedicated daemon volume survived cleanup")
        if outer_docker(host, "ps", "-aq", "--filter", "label=maf-qualification.authz=" + client):
            raise RuntimeError("Dedicated daemon survived cleanup")
    if outer_docker(host, "info", "--format", "{{.ID}}") != parent_id:
        raise RuntimeError("Parent daemon identity changed")
    report["dedicated_daemon_removed"] = True
    report["parent_daemon_unchanged"] = True
    (root / "docker-denial-host-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print("Gateway daemon-denial qualification and dedicated daemon cleanup passed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config", "bicep-config", "openclaw"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--daemon-image", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
