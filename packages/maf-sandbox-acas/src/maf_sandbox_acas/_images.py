"""Resolving what a spec's ``image`` names to something a sandbox can boot from.

An image in a registry and a *disk image* registered in a sandbox group are different
namespaces: a sandbox boots from the latter, and the import is a provisioning step
(``scripts/import_disk_image.py``), never something a user request should block on.  What
happens at runtime is only the lookup below.

There is a **second** namespace the service supplies itself — images it has prebuilt and
keeps Ready for every sandbox group, ``python-3.13`` and ``ubuntu`` among them, which no
one imports.  Microsoft's docs call these *public images* and gloss them as "prebuilt
images available to all sandbox groups", and the same paragraph calls Docker Hub a public
registry; this module says **prebuilt** throughout, because "public image" reads as the
Docker Hub sense to anyone who has not just read that page.  The SDK spells them
``list_public_disk_images()`` / ``begin_create_sandbox(disk=…)``, which is where to look.

:func:`names_a_prebuilt_image` is what tells the two namespaces apart, and it decides which
of the two lookups below a spec gets.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "disk_image_base",
    "names_a_prebuilt_image",
    "qualify_image_reference",
    "resolve_disk_image_id",
    "resolve_prebuilt_image_name",
]

# Resolved disk-image ids, keyed by the configured image reference.  The id is stable for
# the life of an imported image, so one lookup per process is enough.
_disk_image_cache: dict[str, str] = {}

# Prebuilt names already seen in the service's catalogue.  Only hits are remembered: a miss
# stays a miss for one call, so an image the service adds later is found on the next try
# rather than for the life of the process.
_prebuilt_name_cache: set[str] = set()


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


def names_a_prebuilt_image(image: str) -> bool:
    """Is ``image`` a name the service provides, rather than a reference to an import?

    **No registry and no tag.**  A prebuilt image is addressed by a bare name — the version
    is part of the name (``python-3.13``, ``node-22``, ``dotnet-9``), never a tag — while
    everything a host imports arrives here as the ``repository:tag`` this package's whole
    image story is written around, qualified by the configured registry.  So the tag is what
    separates the two, and dropping it from the rule would swallow ``bicep-sandbox:0.46.1``:
    it has no registry either, and every deployment configuring an imported image the way
    :class:`~maf_sandbox.SandboxSpec` documents would silently stop resolving.

    A digest reference (``img@sha256:…``) carries a colon and is excluded with the tags,
    which is right — a digest names something imported, not a catalogue entry.

    >>> names_a_prebuilt_image("python-3.13")
    True
    >>> names_a_prebuilt_image("bicep-sandbox:0.46.1")
    False
    >>> names_a_prebuilt_image("mcr.microsoft.com/devcontainers/python:3.13-bookworm")
    False
    """
    return bool(image) and "/" not in image and ":" not in image


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
            "No sandbox image is configured. Either name an image the service already "
            "provides, as a bare name with no tag (e.g. 'python-3.13'), or set the image "
            "reference that was pushed to the sandbox registry (e.g. "
            "'myacr.azurecr.io/bicep-sandbox:0.46.1') and import it once with "
            "scripts/import_disk_image.py."
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


async def resolve_prebuilt_image_name(group_client: Any, name: str) -> str:
    """Return ``name`` once the service's catalogue is known to hold it.

    The name is what the create call takes, so this function's whole job is to refuse a name
    the service does not have — and to refuse it *here*, naming the catalogue, rather than
    letting the create fail with what the service says about an unknown source.  The most
    likely way to arrive with a bad name is a forgotten tag, and an operator who typed
    ``bicep-sandbox`` for an image they imported needs to be told which namespace they
    landed in, not that a source was rejected.

    Raises:
        ValueError: when the catalogue has no image of that name.  An operator error rather
            than a transient one, so the message carries the whole catalogue.
    """
    if name in _prebuilt_name_cache:
        return name

    available: list[str] = []
    async for image in group_client.list_public_disk_images():
        found = getattr(image, "name", None)
        if not found:
            continue
        available.append(found)
        if found == name:
            _prebuilt_name_cache.add(found)
            return found

    offer = ", ".join(sorted(available)) if available else "nothing — the catalogue is empty"
    raise ValueError(
        f"The sandbox service provides no image named {name!r}. It provides: {offer}. "
        "A bare name with no tag is read as one of those; an image you imported yourself "
        "is named as repository:tag."
    )
