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
from hyperlight_aks import PLUGIN_IMAGE, node_report, plugin_rollout, render_plugin


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

    monkeypatch.setattr(
        build_hyperlight_aks_image, "source_record", lambda **kwargs: {"dirty": True}
    )
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


def labelled_node(**info: str) -> dict[str, Any]:
    labels = {
        "node.kubernetes.io/instance-type": info.pop("size", "Standard_D4ads_v5"),
        "kubernetes.azure.com/node-image-version": info.pop(
            "image", "AKSAzureLinux-V3gen2-202609.15.0"
        ),
        **(
            {"kubernetes.azure.com/security-type": info.pop("security")}
            if "security" in info
            else {}
        ),
    }
    return {
        "metadata": {"name": "node", "labels": labels},
        "status": {
            "allocatable": {"hyperlight.dev/hypervisor": info.pop("allocatable", "1")},
            "nodeInfo": {
                "osImage": "Microsoft Azure Linux 3.0",
                "kernelVersion": "6.6.150.1-1.azl3",
                "containerRuntimeVersion": "containerd://2.2.4",
                "kubeletVersion": "v1.35.7",
                "architecture": "amd64",
                **info,
            },
        },
    }


def test_a_measured_node_platform_is_verified_before_it_is_schedulable():
    report = node_report(labelled_node())
    assert report["verified"] and not report["schedulable"]
    assert report["measured_runc"] == "1.3.6"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"kubeletVersion": "v1.36.1"}, "unmeasured kubelet"),
        ({"kubeletVersion": "v1.35.8"}, "unmeasured kubelet"),
        ({"kernelVersion": "6.6.151.1-1.azl3"}, "unmeasured kernel"),
        ({"containerRuntimeVersion": "containerd://2.2.5"}, "unmeasured runtime"),
        ({"image": "AKSAzureLinux-V3gen2-209912.99.0"}, "unmeasured node_image"),
        ({"image": "AKSAzureLinux-V3gen2TL-202609.15.0"}, "unmeasured node_image"),
        ({"osImage": "Ubuntu 22.04.5 LTS"}, "unmeasured os"),
        ({"size": "Standard_B2s_v2"}, "unmeasured size"),
        ({"security": "TrustedLaunch"}, "security type TrustedLaunch"),
        ({"architecture": "arm64"}, "amd64"),
        ({"allocatable": "0"}, "no hypervisor allocation"),
    ],
)
def test_an_unmeasured_or_unadvertised_node_is_reported(change, reason):
    report = node_report(labelled_node(**change))
    assert not report["verified"]
    assert any(reason in item for item in report["reasons"])


PREVIOUS_PLUGIN = "ghcr.io/hyperlight-dev/hyperlight-device-plugin:51d7dab@sha256:" + "a" * 64


def plugin_daemonset(image: str = PLUGIN_IMAGE) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}


def plugin_pod(node: str = "node", image: str = PLUGIN_IMAGE, **status: Any) -> dict[str, Any]:
    terminating = status.pop("terminating", False)
    return {
        "metadata": {
            "name": f"plugin-{node}",
            **({"deletionTimestamp": "2026-09-26T00:00:00Z"} if terminating else {}),
        },
        "spec": {"nodeName": node, "containers": [{"image": image}]},
        "status": {
            "containerStatuses": [
                {
                    "imageID": "ghcr.io/hyperlight-dev/hyperlight-device-plugin@"
                    + image.rpartition("@")[2],
                    "ready": True,
                    "restartCount": 0,
                    **status,
                }
            ]
        },
    }


def plugin_node(name: str = "node", allocatable: str = "1", **spec: Any) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "spec": spec,
        "status": {"allocatable": {"hyperlight.dev/hypervisor": allocatable}},
    }


def test_a_node_running_the_daemonsets_plugin_is_verified_while_cordoned():
    [report] = plugin_rollout(plugin_daemonset(), [plugin_pod()], [plugin_node(unschedulable=True)])
    assert report["verified"] and report["cordoned"]
    assert report["running_digest"] == PLUGIN_IMAGE.rpartition("@")[2]


def test_ondelete_leaves_the_previous_plugin_running_until_its_pod_is_deleted():
    pods = [plugin_pod("upgraded"), plugin_pod("stale", PREVIOUS_PLUGIN)]
    nodes = [plugin_node("upgraded"), plugin_node("stale")]
    upgraded, stale = plugin_rollout(plugin_daemonset(), pods, nodes)
    assert upgraded["verified"] and not stale["verified"]
    assert any("predates the DaemonSet template" in item for item in stale["reasons"])
    assert any("not the expected one" in item for item in stale["reasons"])


def test_an_explicit_image_checks_a_rollback_before_the_daemonset_is_restored():
    [report] = plugin_rollout(plugin_daemonset(), [plugin_pod()], [plugin_node()], PREVIOUS_PLUGIN)
    assert report["reasons"] == ["the running plugin digest is not the expected one"]


def test_a_template_match_is_not_enough_when_the_node_resolved_another_digest():
    pod = plugin_pod(imageID="ghcr.io/hyperlight-dev/hyperlight-device-plugin@sha256:" + "b" * 64)
    [report] = plugin_rollout(plugin_daemonset(), [pod], [plugin_node()])
    assert report["reasons"] == ["the running plugin digest is not the expected one"]


@pytest.mark.parametrize(
    "pods,node,reason",
    [
        ([], plugin_node(), "0 running plugin pods"),
        ([plugin_pod(), plugin_pod()], plugin_node(), "2 running plugin pods"),
        ([plugin_pod(terminating=True)], plugin_node(), "still terminating"),
        ([plugin_pod(terminating=True), plugin_pod()], plugin_node(), "still terminating"),
        ([plugin_pod(ready=False)], plugin_node(), "not ready"),
        ([plugin_pod("elsewhere")], plugin_node(), "0 running plugin pods"),
        ([plugin_pod()], plugin_node(allocatable="0"), "no hypervisor allocation"),
    ],
)
def test_a_node_without_one_ready_advertising_plugin_is_reported(pods, node, reason):
    [report] = plugin_rollout(plugin_daemonset(), pods, [node])
    assert not report["verified"]
    assert any(reason in item for item in report["reasons"])


def test_the_expected_plugin_image_must_be_digest_pinned():
    with pytest.raises(ValueError, match="digest-pinned"):
        plugin_rollout(plugin_daemonset("plugin:latest"), [], [])
    with pytest.raises(ValueError, match="digest-pinned"):
        plugin_rollout(plugin_daemonset(), [], [], "plugin:latest")
