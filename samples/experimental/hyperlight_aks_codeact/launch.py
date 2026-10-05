"""Supervise the AKS CodeAct sample from a trusted host with scoped Kubernetes credentials."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import re
import sys
from typing import cast

from maf_sandbox import SandboxKey
from maf_sandbox_hyperlight.kubernetes import (
    HyperlightPodCleanupPending,
    HyperlightPodController,
    HyperlightPodTemplate,
)

MODEL_VARS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY")


class SampleController(HyperlightPodController):
    """Add only the model's Secret references to newly created application pods."""

    def __init__(
        self, *, kubeconfig: str, context: str, namespace: str, model_secret: str | None = None
    ) -> None:
        super().__init__(kubeconfig=kubeconfig, context=context, namespace=namespace)
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if model_secret is not None and (
            len(model_secret) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", model_secret)
        ):
            raise ValueError("model_secret must be a Kubernetes Secret name")
        self.model_secret = model_secret

    def api(self, *arguments: str, body: dict[str, object] | None = None) -> dict[str, object]:
        if (
            self.model_secret
            and arguments[:1] == ("create",)
            and body is not None
            and body.get("kind") == "Pod"
        ):
            body = copy.deepcopy(body)
            spec = cast("dict[str, object]", body["spec"])
            [container] = cast("list[dict[str, object]]", spec["containers"])
            env = cast("list[dict[str, object]]", container["env"])
            env.extend(
                {
                    "name": name,
                    "valueFrom": {"secretKeyRef": {"name": self.model_secret, "key": name}},
                }
                for name in MODEL_VARS
            )
        return super().api(*arguments, body=body)


def main(argv: list[str] | None = None) -> int:
    """Retain the caller's ownership key for recovery after interrupted cleanup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "recover"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--agent", default="aks-codeact")
    parser.add_argument("--image", help="Approved application image pinned by sha256 digest.")
    parser.add_argument("--model-secret", help="Existing Secret; enables billable model mode.")
    parser.add_argument("--session-timeout", type=int, default=300)
    args = parser.parse_args(argv)
    if args.action == "run" and not args.image:
        parser.error("run requires --image")
    controller = SampleController(
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=args.namespace,
        model_secret=args.model_secret,
    )
    key = SandboxKey(args.scope, args.thread, args.agent)
    try:
        if args.action == "recover":
            result = controller.recover_exit(key, "codeact", retire=True)
        else:
            command = ("python", "-u", "/opt/aks-codeact/agent.py")
            if args.model_secret:
                command += ("--model",)
            result = controller.supervise(
                key,
                "codeact",
                HyperlightPodTemplate(args.image, command, session_timeout=args.session_timeout),
            )
    except HyperlightPodCleanupPending:
        print(
            "Cleanup pending: preserve this scope/thread/agent and use recover. "
            "Do not remove ownership records or start a replacement for this session.",
            file=sys.stderr,
        )
        return 3
    print(json.dumps(dataclasses.asdict(result), indent=2))
    return 0 if result.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
