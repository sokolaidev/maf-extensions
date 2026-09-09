"""Import an OCI image into an ACA sandbox group as a disk image.

An existing reference is refused: a disk image is a snapshot, and comparing reference
strings cannot tell whether a tag's contents have changed. Use a new tag for each build.

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
        help="Managed identity resource id for the pull (preview service support varies).",
    )
    parser.add_argument("--username", help="Registry username; requires a token.")
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument("--token", help="Registry token; requires --username.")
    token_source.add_argument(
        "--token-stdin", action="store_true", help="Read the registry token from standard input."
    )
    args = parser.parse_args(argv)
    has_token = args.token is not None or args.token_stdin
    if args.identity is not None and (args.username is not None or has_token):
        parser.error("--identity cannot be combined with --username or a registry token")
    if (args.username is not None) != has_token:
        parser.error("--username and either --token or --token-stdin must be supplied together")
    if args.token_stdin:
        args.token = sys.stdin.read().strip()
    if args.username is not None and (
        not args.username.strip() or args.token is None or not args.token.strip()
    ):
        parser.error("registry username and token must not be empty")
    return args


def _default_name(image_ref: str) -> str:
    """A stable, readable disk-image name derived from the reference's repo and tag."""
    tail = image_ref.rsplit("/", 1)[-1]
    return tail.replace(":", "-").replace("@", "-")[:60]


async def _run(args: argparse.Namespace) -> int:
    try:
        from azure.containerapps.sandbox import RegistryCredentials
        from azure.containerapps.sandbox.aio import SandboxGroupClient
        from azure.identity.aio import DefaultAzureCredential
    except ImportError:
        print(
            "azure-containerapps-sandbox is not installed. Run: uv sync --package maf-sandbox-acas",
            file=sys.stderr,
        )
        return 2

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
                print(
                    f"Nothing imported: {args.image!r} already has a disk image ({image.id}). "
                    "Its snapshot does not change when the tag is overwritten. "
                    "Push and import a new build tag, or pin the existing disk-image id "
                    "explicitly if you intend to reuse it.",
                    file=sys.stderr,
                )
                return 1

        print(f"importing {args.image} …", file=sys.stderr)
        poller: Any = await client.begin_create_disk_image(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            args.image,
            name=args.name or _default_name(args.image),
            managed_identity_resource_id=args.identity or None,
            registry_credentials=(
                RegistryCredentials(username=args.username, token=args.token)
                if args.username is not None
                else None
            ),
        )
        image: Any = await poller.result()
        print(image.id)
        return 0
    finally:
        try:
            await client.close()
        finally:
            await credential.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point: import the image and print its disk-image id."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
