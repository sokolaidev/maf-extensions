"""The build context and upstream overlay preserve their explicit supply-chain inputs."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_hyperlight_aks_image
from hyperlight_aks import PLUGIN_IMAGE, render_plugin


@pytest.mark.parametrize("namespace", ["", "-a", "a-", "a.b", "A", "a" * 64])
def test_overlay_rejects_invalid_namespace_before_rendering(namespace):
    with pytest.raises(ValueError, match="namespace"):
        render_plugin("", namespace=namespace)


@pytest.mark.parametrize("count", [True, False, 1.5, "1", 0, 2001])
def test_overlay_refuses_noninteger_or_unbounded_device_counts(count: Any):
    with pytest.raises(ValueError, match="device count"):
        render_plugin("", namespace="infra", count=count)


def test_bundle_build_accepts_uv_metadata_and_hashes_the_exact_payload(tmp_path, monkeypatch):
    def execute(command, **kwargs):
        if command[1] == "build":
            wheels = Path(command[command.index("--out-dir") + 1])
            wheels.mkdir(exist_ok=True)
            (wheels / ".gitignore").write_text("*")
            package = command[command.index("--package") + 1].replace("-", "_")
            (wheels / f"{package}-1-py3-none-any.whl").write_bytes(b"wheel")
        else:
            Path(command[command.index("--output-file") + 1]).write_text("# locked dependencies")

    monkeypatch.setattr(subprocess, "run", execute)
    build_hyperlight_aks_image.prepare(tmp_path)
    bundle = json.loads(gzip.decompress((tmp_path / "bundle.json.gz").read_bytes()))
    assert len(bundle["wheels"]) == 3
    assert set(bundle["files"]) == {"requirements.txt", "probe.py"}
    evidence = json.loads((tmp_path / "build-inputs.json").read_text())
    assert evidence["probe.py"] == hashlib.sha256((tmp_path / "probe.py").read_bytes()).hexdigest()
    assert (tmp_path / ".dockerignore").read_text().startswith("**\n")
    (tmp_path / "credentials.txt").write_text("unrelated")
    with pytest.raises(ValueError, match="unrelated"):
        build_hyperlight_aks_image.prepare(tmp_path)


@pytest.mark.parametrize("namespace,count", [("trusted-infra", 1), ("a", 2000), ("a" * 63, 1)])
def test_overlay_keeps_upstream_devices_without_adding_node_delegation(namespace, count):
    source = {
        "kind": "DaemonSet",
        "metadata": {"name": "hyperlight-device-plugin"},
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": {"hyperlight.dev/enabled": "true"},
                    "volumes": [{"name": "cdi", "hostPath": {"path": "/var/run/cdi"}}],
                    "containers": [
                        {
                            "image": "${IMAGE}",
                            "env": [{"name": "DEVICE_COUNT", "value": "${DEVICE_COUNT}"}],
                            "securityContext": {"runAsUser": 0, "privileged": False},
                        }
                    ],
                }
            }
        },
    }
    result = render_plugin(json.dumps(source), namespace=namespace, count=count)
    daemon = result["items"][0]
    pod = daemon["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == source["spec"]["template"]["spec"]["nodeSelector"]
    assert pod["volumes"] == source["spec"]["template"]["spec"]["volumes"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["image"] == PLUGIN_IMAGE
    assert pod["containers"][0]["env"][0]["value"] == str(count)
    assert pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert daemon["spec"]["updateStrategy"] == {"type": "OnDelete"}
    with pytest.raises(ValueError, match="digest"):
        render_plugin(json.dumps(source), namespace="trusted-infra", image="plugin:latest")
