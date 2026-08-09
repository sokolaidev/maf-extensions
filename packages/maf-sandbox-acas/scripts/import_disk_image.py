"""Import the bicep-sandbox OCI image into an ACA sandbox group as a disk image.

``deploy-bicep-sandbox.yml`` builds and pushes ``bicep-sandbox:<version>`` to the sandbox
stack's **own** registry, but a sandbox boots from a *disk image* registered in the sandbox
group, which is a different namespace.  This script closes that gap: it imports the pushed
image once, so the host application can then resolve it by reference at runtime
(:func:`maf_sandbox_acas.resolve_disk_image_id`).

Importing is deliberately kept out of the request path — it is slow, it is a write against
the group, and it needs registry credentials that the application itself has no reason to
hold.

The **deploy workflow no longer runs this**: it uses the vendor's ``aca`` CLI
(``aca sandboxgroup disk create --identity …``), which needs no Python toolchain and nothing
from this repository.  This script stays as the equivalent for anyone who would rather not
install the CLI, and because its idempotency check shares
:func:`~maf_sandbox_acas.disk_image_base` with the runtime resolver.

See ``scripts/README.md`` for how to run it, the arguments, and authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--endpoint", required=True, help="Sandbox group data-plane endpoint.")
    parser.add_argument("--subscription", required=True, help="Subscription id of the group.")
    parser.add_argument("--resource-group", required=True, help="Resource group of the group.")
    parser.add_argument("--group", required=True, help="Sandbox group name.")
    parser.add_argument("--image", required=True, help="OCI image reference to import.")
    parser.add_argument(
        "--name", default=None, help="Disk image name (default: derived from the tag)."
    )
    parser.add_argument(
        "--identity",
        default=None,
        help="Managed identity resource id with AcrPull, for a private registry.",
    )
    return parser.parse_args(argv)


def _default_name(image_ref: str) -> str:
    """A stable, readable disk-image name derived from the reference's repo and tag."""
    tail = image_ref.rsplit("/", 1)[-1]
    return tail.replace(":", "-").replace("@", "-")[:60]


async def _run(args: argparse.Namespace) -> int:
    try:
        from azure.containerapps.sandbox.aio import SandboxGroupClient
        from azure.identity.aio import DefaultAzureCredential
    except ImportError:
        print(
            "azure-containerapps-sandbox is not installed. Run: uv sync --package maf-sandbox-acas",
            file=sys.stderr,
        )
        return 2

    # The same accessor the runtime resolver uses.  `DiskImage.image` is a DiskImageSpec,
    # not a string, so comparing it directly to the reference never matches and the
    # idempotency check below would silently re-import on every run.
    from maf_sandbox_acas import disk_image_base

    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint=args.endpoint,
        credential=credential,
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        sandbox_group=args.group,
    )
    try:
        async for image in client.list_disk_images():
            if disk_image_base(image) == args.image:
                print(f"already imported: {image.id}")
                return 0

        print(f"importing {args.image} …", file=sys.stderr)
        # Spelled out rather than splatted from a dict: `**kwargs` erases the argument
        # types, so a typo in a keyword — or a value of the wrong type — would only show
        # up as a service error during a slow LRO.  `managed_identity_resource_id=None` is
        # the SDK's own default, so passing it unconditionally changes nothing.
        # `Any`-annotated on purpose: azure-containerapps-sandbox is a 0.1.0bN preview that
        # ships no type information, so a strict checker reports every value that comes back
        # from it as unknown. Naming the type here says "untyped SDK boundary" once, instead
        # of leaving five reportUnknown* findings for a reader to re-derive.
        poller: Any = await client.begin_create_disk_image(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            args.image,
            name=args.name or _default_name(args.image),
            managed_identity_resource_id=args.identity or None,
        )
        image: Any = await poller.result()
        print(image.id)
        return 0
    finally:
        await client.close()
        await credential.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point: import the image and print its disk-image id."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
