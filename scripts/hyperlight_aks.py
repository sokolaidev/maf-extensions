"""Render the pinned upstream plugin overlay, report eligible nodes and plugin rollout, or supervise one pod."""

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


def _digest(image: str) -> str:
    match = re.search(r"@(sha256:[a-f0-9]{64})$", image)
    return match.group(1) if match else ""


def _owned_by(resource: dict, owner: dict) -> bool:
    return any(
        reference.get("controller") is True and reference.get("uid") == owner["metadata"]["uid"]
        for reference in resource["metadata"].get("ownerReferences", [])
    )


def current_revision(daemonset: dict, revisions: list[dict]) -> str:
    """The DaemonSet's newest ControllerRevision hash, which its up-to-date pods carry.

    Read `revisions` after `daemonset`: until the controller has observed the DaemonSet's
    generation, its newest revision may still be the previous template's.
    """
    observed = daemonset.get("status", {}).get("observedGeneration")
    if observed != daemonset["metadata"].get("generation"):
        raise ValueError("the DaemonSet controller has not observed its latest template; retry")
    owned = [item for item in revisions if _owned_by(item, daemonset)]
    if not owned:
        raise ValueError("the DaemonSet owns no ControllerRevision")
    newest = max(owned, key=lambda item: item["revision"])
    return newest["metadata"].get("labels", {}).get("controller-revision-hash", "")


def _plugin_pod(pod: dict, daemonset: dict, revision: str) -> dict:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    status = statuses[0] if len(statuses) == 1 else {}
    return {
        "name": pod["metadata"]["name"],
        "controlled": _owned_by(pod, daemonset),
        "terminating": bool(pod["metadata"].get("deletionTimestamp")),
        "current_revision": pod["metadata"].get("labels", {}).get("controller-revision-hash")
        == revision,
        "template_image": pod["spec"]["containers"][0]["image"],
        "running_digest": _digest(status.get("imageID", "")),
        "ready": status.get("ready") is True,
        "restarts": status.get("restartCount", 0),
    }


def plugin_rollout(
    daemonset: dict, revision: str, pods: list[dict], nodes: list[dict], image: str = ""
) -> list:
    """Report every plugin pod on each plugin-enabled node; `OnDelete` never replaces one."""
    template = daemonset["spec"]["template"]["spec"]["containers"][0]["image"]
    expected = _digest(image or template)
    if not expected:
        raise ValueError("the expected plugin image must be digest-pinned")
    if not revision:
        raise ValueError("the DaemonSet's current revision is unknown")
    reports = []
    for node in nodes:
        name = node["metadata"]["name"]
        here = [
            _plugin_pod(pod, daemonset, revision)
            for pod in pods
            if pod["spec"].get("nodeName") == name
        ]
        placed = [pod for pod in here if pod["controlled"]]
        live = [pod for pod in placed if not pod["terminating"]]
        allocatable = node["status"].get("allocatable", {}).get("hyperlight.dev/hypervisor", "0")
        reasons = []
        if len(placed) < len(here):
            reasons.append("a plugin-labelled pod is not controlled by the DaemonSet")
        if len(live) < len(placed):
            reasons.append("a plugin pod is still terminating")
        if len(live) != 1:
            reasons.append(f"{len(live)} running plugin pods, expected one")
        else:
            [pod] = live
            # A rollback target is checked before the DaemonSet is restored, so its pods
            # are necessarily on an older revision.
            if not image and not pod["current_revision"]:
                reasons.append("the plugin pod predates the DaemonSet template; delete it")
            if pod["running_digest"] != expected:
                reasons.append("the running plugin digest is not the expected one")
            if not pod["ready"]:
                reasons.append("the plugin pod is not ready")
        if allocatable in {"", "0"}:
            reasons.append("the device plugin advertises no hypervisor allocation")
        reports.append(
            {
                "name": name,
                "cordoned": bool(node["spec"].get("unschedulable")),
                "allocatable": allocatable,
                "expected_digest": expected,
                "pods": here,
                "verified": not reasons,
                "reasons": reasons,
            }
        )
    return reports


def _kubectl(args: argparse.Namespace, *command: str) -> dict:
    listed = subprocess.run(
        ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(listed.stdout)


def main() -> None:
    """Kubernetes authentication stays with the operator or host controller's kubeconfig."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("plugin", "plugin-status", "nodes", "supervise", "recover")
    )
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
    parser.add_argument("--recovery-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.action == "nodes":
        if not (args.kubeconfig and args.context):
            parser.error("nodes requires kubeconfig and context")
        listed = _kubectl(args, "get", "nodes", "-l", "hyperlight.dev/enabled=true", "-o", "json")
        reports = [node_report(node) for node in listed["items"]]
        print(json.dumps(reports, indent=2))
        raise SystemExit(0 if reports and all(item["verified"] for item in reports) else 1)
    if not args.namespace:
        parser.error(f"{args.action} requires --namespace")
    if args.action == "plugin-status":
        if not (args.kubeconfig and args.context):
            parser.error("plugin-status requires kubeconfig and context")
        scope = ("-n", args.namespace, "-o", "json")
        selector = ("-l", "app.kubernetes.io/name=hyperlight-device-plugin")
        daemonset = _kubectl(args, "get", "daemonset", "hyperlight-device-plugin", *scope)
        reports = plugin_rollout(
            daemonset,
            current_revision(
                daemonset,
                _kubectl(args, "get", "controllerrevisions", *selector, *scope)["items"],
            ),
            _kubectl(args, "get", "pods", *selector, *scope)["items"],
            _kubectl(args, "get", "nodes", "-l", "hyperlight.dev/enabled=true", "-o", "json")[
                "items"
            ],
            args.image or "",
        )
        print(json.dumps(reports, indent=2))
        raise SystemExit(0 if reports and all(item["verified"] for item in reports) else 1)
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
            recovery_seconds=args.recovery_seconds,
        ),
    )
    print(json.dumps(dataclasses.asdict(result), indent=2))
    raise SystemExit(0 if result.exit_code == 0 else 1)


if __name__ == "__main__":
    main()
