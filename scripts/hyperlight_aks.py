"""Render the pinned upstream device-plugin overlay or supervise one scoped application pod."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import urllib.request

import yaml
from maf_sandbox import SandboxKey
from maf_sandbox_hyperlight.kubernetes import HyperlightPodController, HyperlightPodTemplate

UPSTREAM_REVISION = "fc71b4501d23977fcc54f7be144d884fc8210667"
PLUGIN_IMAGE = "ghcr.io/hyperlight-dev/hyperlight-device-plugin:fc71b45@sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98"
UPSTREAM_MANIFEST = f"https://raw.githubusercontent.com/hyperlight-dev/hyperlight-on-kubernetes/{UPSTREAM_REVISION}/deploy/manifests/device-plugin.yaml"


def render_plugin(source: str, *, namespace: str, image: str = PLUGIN_IMAGE, count: int = 1):
    """Keep upstream's plugin and CDI paths while pinning deployment and security settings."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", namespace):
        raise ValueError("invalid infrastructure namespace")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) or not 1 <= count <= 2000:
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


def main() -> None:
    """Kubernetes authentication stays with the operator or host controller's kubeconfig."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plugin", "run", "recover"))
    parser.add_argument("--namespace", required=True)
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
        parser.error("run/recover require kubeconfig, context and the complete host-owned scope")
    controller = HyperlightPodController(
        kubeconfig=args.kubeconfig, context=args.context, namespace=args.namespace
    )
    key = SandboxKey(args.scope, args.thread, args.agent)
    if args.action == "recover":
        print(json.dumps({"exit_code": controller.recover(key, args.kind, retire=True)}))
        return
    if not args.image:
        parser.error("run requires the built application image digest")
    result = controller.run(
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
