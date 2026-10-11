"""CodeAct cannot capture a callable surface with unsupported required FIDES coverage."""

import pytest
from maf_sandbox import (
    CallerContext,
    Capability,
    HostToolFidesNotSupported,
    HostToolRegistry,
    SandboxRouter,
    sandbox_tool,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend
from maf_sandbox_codeact import codeact_sandbox_spec, make_codeact_tools


@sandbox_tool(source=None, sink=None, identity=None)
def _pure() -> int:
    return 1


def _tools(registry, **options):
    backend = InProcessSandboxBackend()
    return make_codeact_tools(
        SandboxRouter([backend], min_isolation=backend.isolation),
        "agent",
        CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        host_tools=registry,
        **options,
    )


@pytest.mark.parametrize("build", [codeact_sandbox_spec, lambda **kw: _tools(kw["host_tools"])])
def test_required_fides_refuses_surface_before_backend_selection(build):
    registry = HostToolRegistry(require_fides=True, require_declared=True)
    registry.register(_pure)
    with pytest.raises(HostToolFidesNotSupported, match="FIDES enforcement is required"):
        build(host_tools=registry)


def test_disabled_channel_ignores_strict_registry_and_late_registration():
    registry = HostToolRegistry(require_fides=True)
    registry.register(_pure)
    tools = _tools(registry, host_tools_enabled=False)
    spec = codeact_sandbox_spec(host_tools=registry, host_tools_enabled=False)
    registry.register(lambda: None, name="late")
    assert spec.host_tools is None
    assert Capability.HOST_TOOLS not in spec.requires
    assert "import maf_host_tools" not in tools[0].description


def test_empty_strict_surface_cannot_gain_tools_after_capture():
    registry = HostToolRegistry(require_fides=True)
    assert _tools(registry)
    with pytest.raises(ValueError, match="sealed"):
        registry.register(_pure)
