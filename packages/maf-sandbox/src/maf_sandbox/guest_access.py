"""Acquire-time refusals based on a backend's established file-access facts."""

from ._protocol import Capability, SandboxSpec
from ._router import SandboxCapabilityNotSupported


def refuse_capabilities_the_guest_cannot_back(
    spec: SandboxSpec, *, files_land_as_guest: bool, backend_name: str, detail: str = ""
) -> None:
    """Refuse writing-guest capabilities unless the backend establishes guest file access.

    The fact may come from ownership, ACLs, or the platform's file-plane semantics; an
    unknown answer is False. This helper neither probes a guest nor changes permissions.
    """
    refused = spec.requires & {Capability.FILES_OUT, Capability.HOST_TOOLS}
    if refused and not files_land_as_guest:
        raise SandboxCapabilityNotSupported(
            f"sandbox backend {backend_name!r} cannot serve "
            f"{', '.join(sorted(refused))} to the {spec.kind!r} workload: "
            "guest access to file-plane inputs has not been established; the guest may "
            "be unable to write outputs, host-tool markers, or empty its call directory. "
            f"{detail}"
        )
