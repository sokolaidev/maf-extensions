"""Resolving an OCI reference to an imported ACA disk image.

An image in a registry and a *disk image* registered in a sandbox group are different
namespaces: a sandbox boots from the latter, and the import is a provisioning step
(``scripts/import_disk_image.py``), never something a user request should block on.  What
happens at runtime is only the lookup below.
"""

from __future__ import annotations

from typing import Any

__all__ = ["disk_image_base", "qualify_image_reference", "resolve_disk_image_id"]

# Resolved disk-image ids, keyed by the configured image reference.  The id is stable for
# the life of an imported image, so one lookup per process is enough.
_disk_image_cache: dict[str, str] = {}


def disk_image_base(image: Any) -> str | None:
    """Return the OCI reference a listed disk image was built from, or ``None``.

    ``DiskImage.image`` is a :class:`~azure.containerapps.sandbox.DiskImageSpec` dataclass
    (``base`` / ``entrypoint`` / ``cmd``), **not** the reference string.  Comparing the
    object itself to a reference silently never matches, which would make every
    resolve-by-reference lookup fail with "no disk image was built from …" even directly
    after a successful import.  A plain string is tolerated too, in case a future SDK
    version flattens the field.

    Shared with ``scripts/import_disk_image.py`` so the runtime lookup and the operator
    script's idempotency check can never disagree about the shape.
    """
    spec = getattr(image, "image", None)
    if spec is None:
        return None
    if isinstance(spec, str):
        return spec or None
    base = getattr(spec, "base", None)
    return base if isinstance(base, str) and base else None


def qualify_image_reference(registry: str, image: str) -> str:
    """Prefix ``image`` with the backend's registry, unless it already names one.

    A sandbox *kind* declares ``repository:tag`` — ``bicep-sandbox:0.46.1`` — because
    which repository holds its image is a property of the workload, while *where* images
    live is a property of the deployment.  Splitting them means a kind's configuration does
    not change when the registry does, and it is what lets one registry serve every kind.

    A fully-qualified reference is passed through untouched.  That is not politeness: the
    alternative is silently producing ``acr.io/acr.io/img``, whose only symptom is
    "no disk image was built from …" — a message that sends an operator looking at the
    import step rather than at the value they pasted.

    The test for "already qualified" is the OCI one: a first path segment containing a dot
    or a colon, or exactly ``localhost``, is a registry host.  It only applies when there is
    a path separator at all, or ``bicep-sandbox:0.46.1`` would read as a host because of
    its tag.

    >>> qualify_image_reference("acr.azurecr.io", "bicep-sandbox:0.46.1")
    'acr.azurecr.io/bicep-sandbox:0.46.1'
    >>> qualify_image_reference("acr.azurecr.io", "other.azurecr.io/img:1")
    'other.azurecr.io/img:1'
    >>> qualify_image_reference("acr.azurecr.io", "library/ubuntu:22.04")
    'acr.azurecr.io/library/ubuntu:22.04'
    """
    if not image or not registry:
        return image
    head, separator, _ = image.partition("/")
    if separator and ("." in head or ":" in head or head == "localhost"):
        return image
    return f"{registry.rstrip('/')}/{image}"


async def resolve_disk_image_id(
    group_client: Any, explicit_id: str | None, image_ref: str | None
) -> str:
    """Return the id of the disk image to boot.

    ``explicit_id`` wins when set.  Otherwise the group's imported disk images are searched
    for one built from ``image_ref``, so an operator configures a human-readable image
    reference rather than pasting an opaque id.

    Raises:
        ValueError: when neither is set, or when the reference was never imported. Both are
            operator errors rather than transient ones, so they carry an actionable message
            a caller can safely surface.
    """
    if explicit_id:
        return explicit_id
    if not image_ref:
        raise ValueError(
            "No sandbox image is configured. Set the image reference that was pushed to the "
            "sandbox registry (e.g. 'myacr.azurecr.io/bicep-sandbox:0.46.1') and import "
            "it once with scripts/import_disk_image.py."
        )

    cached = _disk_image_cache.get(image_ref)
    if cached is not None:
        return cached

    async for image in group_client.list_disk_images():
        if disk_image_base(image) == image_ref:
            image_id = getattr(image, "id", None)
            if image_id:
                _disk_image_cache[image_ref] = image_id
                return image_id

    raise ValueError(
        f"No disk image in the sandbox group was built from {image_ref!r}. "
        "Import it once with scripts/import_disk_image.py, or pin a disk-image id "
        "explicitly."
    )
