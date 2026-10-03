"""Default shared files and program staging must fit declared backend ceilings."""

import pytest
from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    CallerContext,
    SandboxRouter,
    SandboxTransferLimitsNotPermitted,
)
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_codeact import codeact_sandbox_spec, make_codeact_tools
from maf_sandbox_docker_sbx import SbxSandboxBackend
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig


@pytest.mark.parametrize("backend", [WslcSandboxBackend(WslcSandboxConfig()), SbxSandboxBackend()])
def test_default_file_store_admission_preserves_explicit_limits(backend):
    router = SandboxRouter([backend], min_isolation=backend.isolation)
    context = CallerContext(
        current_scope=lambda: "scope",
        current_thread_id=lambda: "thread",
        list_files=InMemoryStore.list,
    )
    assert make_codeact_tools(router, "agent", context, file_store=InMemoryStore({}))
    spec = codeact_sandbox_spec(takes_files=True)
    folded = backend.declarations.program_channels[0].transfer_limits(spec)
    assert folded.files_in.within(backend.declarations.limits.files_in)
    assert spec.files_in.max_total_bytes == 24 * 1024 * 1024
    assert spec.files_in.max_files == 63
    with pytest.raises(SandboxTransferLimitsNotPermitted):
        make_codeact_tools(
            router, "agent", context, file_store=InMemoryStore({}), files_in=DEFAULT_TRANSFER_LIMITS
        )
