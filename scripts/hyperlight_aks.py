"""Render the pinned upstream plugin overlay, report eligible nodes, or supervise one pod."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import urllib.request

import yaml
from maf_sandbox import SandboxKey
from maf_sandbox_hyperlight.kubernetes import HyperlightPodController, HyperlightPodTemplate

UPSTREAM_REVISION = "fc71b4501d23977fcc54f7be144d884fc8210667"
PLUGIN_IMAGE = "ghcr.io/hyperlight-dev/hyperlight-device-plugin:fc71b45@sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98"
# Exact observations from live probe runs. A node verifies only by matching one of them;
# a newer node image, kernel or patch needs its own run first. Node status omits runc, so
# the report shows the measured value for the operator to compare on the node.
MEASURED_PLATFORMS = (
    {
        "size": "Standard_D4ads_v5",
        "node_image": "AKSUbuntu-2404gen2containerd-202609.15.0",
        "os": "Ubuntu 24.04.5 LTS",
        "kernel": "6.8.0-1067-azure",
        "kubelet": "v1.35.7",
        "runtime": "containerd://2.3.3-2",
        "runc": "1.4.3-2",
    },
    {
        "size": "Standard_D4ads_v5",
        "node_image": "AKSUbuntu-2404gen2containerd-202609.09.0",
        "os": "Ubuntu 24.04.5 LTS",
        "kernel": "6.8.0-1067-azure",
        "kubelet": "v1.35.7",
        "runtime": "containerd://2.3.3-2",
        "runc": "1.4.3-2",
    },
    {
        "size": "Standard_D4ads_v5",
        "node_image": "AKSAzureLinux-V3gen2-202609.15.0",
        "os": "Microsoft Azure Linux 3.0",
        "kernel": "6.6.150.1-1.azl3",
        "kubelet": "v1.35.7",
        "runtime": "containerd://2.2.4",
        "runc": "1.3.6",
    },
)
UPSTREAM_MANIFEST = f"https://raw.githubusercontent.com/hyperlight-dev/hyperlight-on-kubernetes/{UPSTREAM_REVISION}/deploy/manifests/device-plugin.yaml"


def render_plugin(source: str, *, namespace: str, image: str = PLUGIN_IMAGE, count: int = 1):
    """Keep upstream's plugin and CDI paths while pinning deployment and security settings."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("invalid infrastructure namespace")
    if (
        not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image)
        or type(count) is not int
        or not 1 <= count <= 2000
    ):
        raise ValueError("a digest-pinned image and bounded device count are required")
    for name, value in {
        "IMAGE": image,
        "DEVICE_COUNT": str(count),
        "DEVICE_UID": "65534",
        "DEVICE_GID": "65534",
    }.items():
        source = source.replace("${" + name + "}", value)
    if "${" in source:
        raise ValueError("unresolved upstream device-plugin placeholder")
    resources = list(yaml.safe_load_all(source))
    daemonsets = 0
    for resource in resources:
        if resource["kind"] == "Namespace":
            resource["metadata"]["name"] = namespace
        else:
            resource["metadata"]["namespace"] = namespace
        if resource["kind"] == "DaemonSet":
            daemonsets += 1
            resource["spec"]["updateStrategy"] = {"type": "OnDelete"}
            pod = resource["spec"]["template"]["spec"]
            pod["automountServiceAccountToken"] = False
            pod["securityContext"] = {"seccompProfile": {"type": "RuntimeDefault"}}
            for container in pod["containers"]:
                container["securityContext"]["capabilities"] = {"drop": ["ALL"]}
                container["imagePullPolicy"] = "IfNotPresent"
    if daemonsets != 1:
        raise ValueError("the pinned source must contain exactly one device-plugin DaemonSet")
    return {"apiVersion": "v1", "kind": "List", "items": resources}


def node_report(node: dict) -> dict:
    """Compare one plugin-enabled node with the verified matrix, before it becomes schedulable."""
    info = node["status"]["nodeInfo"]
    labels = node["metadata"].get("labels", {})
    observed = {
        "name": node["metadata"]["name"],
        "size": labels.get("node.kubernetes.io/instance-type", ""),
        "node_image": labels.get("kubernetes.azure.com/node-image-version", ""),
        "security_type": labels.get("kubernetes.azure.com/security-type", ""),
        "os": info["osImage"],
        "kernel": info["kernelVersion"],
        "runtime": info["containerRuntimeVersion"],
        "kubelet": info["kubeletVersion"],
        "architecture": info["architecture"],
        "allocatable": node["status"].get("allocatable", {}).get("hyperlight.dev/hypervisor", "0"),
        "schedulable": labels.get("hyperlight.dev/hypervisor") == "kvm",
    }
    reasons = []
    if observed["architecture"] != "amd64":
        reasons.append("architecture is not amd64")
    if observed["security_type"]:
        reasons.append(f"security type {observed['security_type']} is not measured")
    nearest = min(
        MEASURED_PLATFORMS,
        key=lambda row: sum(observed[field] != row[field] for field in row if field != "runc"),
    )
    differences = [f for f in nearest if f != "runc" and observed[f] != nearest[f]]
    if differences:
        reasons.append(f"unmeasured {', '.join(differences)}; nearest measured platform differs")
    if observed["allocatable"] in {"", "0"}:
        reasons.append("the device plugin advertises no hypervisor allocation")
    return {
        **observed,
        "measured_runc": nearest["runc"],
        "verified": not reasons,
        "reasons": reasons,
    }


def main() -> None:
    """Kubernetes authentication stays with the operator or host controller's kubeconfig."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plugin", "nodes", "supervise", "recover"))
    parser.add_argument("--namespace")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context")
    parser.add_argument("--image")
    parser.add_argument("--scope")
    parser.add_argument("--thread")
    parser.add_argument("--agent")
    parser.add_argument("--kind", default="codeact")
    parser.add_argument("--device-count", type=int, default=1)
    parser.add_argument("--mode", default="positive")
    parser.add_argument("--bundle-configmap")
    parser.add_argument("--bundle-sha256")
    args = parser.parse_args()
    if args.action == "nodes":
        if not (args.kubeconfig and args.context):
            parser.error("nodes requires kubeconfig and context")
        listed = subprocess.run(
            ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
            + ["get", "nodes", "-l", "hyperlight.dev/enabled=true", "-o", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        reports = [node_report(node) for node in json.loads(listed.stdout)["items"]]
        print(json.dumps(reports, indent=2))
        raise SystemExit(0 if reports and all(item["verified"] for item in reports) else 1)
    if not args.namespace:
        parser.error(f"{args.action} requires --namespace")
    if args.action == "plugin":
        with urllib.request.urlopen(UPSTREAM_MANIFEST, timeout=20) as response:
            source = response.read(1024 * 1024).decode()
        print(
            json.dumps(
                render_plugin(
                    source,
                    namespace=args.namespace,
                    image=args.image or PLUGIN_IMAGE,
                    count=args.device_count,
                ),
                indent=2,
            )
        )
        return
    if not all((args.kubeconfig, args.context, args.scope, args.thread, args.agent)):
        parser.error(
            "supervise/recover require kubeconfig, context and the complete host-owned scope"
        )
    controller = HyperlightPodController(
        kubeconfig=args.kubeconfig, context=args.context, namespace=args.namespace
    )
    key = SandboxKey(args.scope, args.thread, args.agent)
    if args.action == "recover":
        print(json.dumps(dataclasses.asdict(controller.recover_exit(key, args.kind, retire=True))))
        return
    if not args.image:
        parser.error("supervise requires the built application image digest")
    result = controller.supervise(
        key,
        args.kind,
        HyperlightPodTemplate(
            args.image,
            (
                "python",
                "-I",
                "-u",
                "/work/probe.py" if args.bundle_configmap else "/opt/hyperlight-probe.py",
                args.mode,
            ),
            bundle_configmap=args.bundle_configmap,
            bundle_sha256=args.bundle_sha256,
        ),
    )
    print(json.dumps(dataclasses.asdict(result), indent=2))
    raise SystemExit(0 if result.exit_code == 0 else 1)


if __name__ == "__main__":
    main()
