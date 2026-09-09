"""The guest file-access refusal is independent of an operating system's principal model."""

import pytest

from maf_sandbox import Capability, SandboxCapabilityNotSupported, SandboxSpec
from maf_sandbox.guest_access import refuse_capabilities_the_guest_cannot_back


@pytest.mark.parametrize("capability", [Capability.FILES_OUT, Capability.HOST_TOOLS])
def test_unestablished_access_refuses_writing_capabilities(capability):
    spec = SandboxSpec(kind="windows-tool", requires=frozenset({capability}))
    with pytest.raises(SandboxCapabilityNotSupported, match=capability.value):
        refuse_capabilities_the_guest_cannot_back(
            spec, files_land_as_guest=False, backend_name="acl-backend", detail="Check the ACL."
        )


@pytest.mark.parametrize("capability", list(Capability))
def test_established_access_accepts_capabilities(capability):
    if capability is Capability.RECLAIM:
        return
    spec = SandboxSpec(kind="tool", requires=frozenset({capability}))
    refuse_capabilities_the_guest_cannot_back(
        spec, files_land_as_guest=True, backend_name="backend"
    )


def test_stdout_workloads_do_not_require_file_access():
    refuse_capabilities_the_guest_cannot_back(
        SandboxSpec(kind="stdout", requires=frozenset({Capability.EXEC})),
        files_land_as_guest=False,
        backend_name="backend",
    )
