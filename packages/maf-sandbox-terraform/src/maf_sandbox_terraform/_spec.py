"""Host-selected engine and the boundary required to execute its providers."""

from typing import Literal

from maf_sandbox import Cleanup, Egress, Isolation, IsolationScope, OsFamily, SandboxSpec

TerraformEngine = Literal["terraform", "opentofu"]
TERRAFORM_TOOL_NAMES: frozenset[str] = frozenset({"terraform_validate", "opentofu_validate"})


def checked_engine(engine: TerraformEngine) -> TerraformEngine:
    """Refuse aliases and fallback selection, including at untyped call sites."""
    if engine not in ("terraform", "opentofu"):
        raise ValueError("engine must be 'terraform' or 'opentofu'")
    return engine


def terraform_sandbox_spec(
    image: str | None = None,
    image_id: str | None = None,
    *,
    engine: TerraformEngine = "terraform",
) -> SandboxSpec:
    """Require a fresh POSIX sandbox with closed egress and disposal after each call.

    Images must supply the fixed launcher described in the package README. Provider code and
    configuration expressions can access other guest paths, so this kind makes no claim of
    confinement to the call directory and does not support warm reuse.
    """
    return SandboxSpec(
        kind=checked_engine(engine),
        image=image,
        image_id=image_id,
        work_dir=None,
        egress=Egress.CLOSED,
        requires_os_family=OsFamily.POSIX,
        min_isolation=Isolation.CONTAINER,
        isolation_scope=IsolationScope.CALL,
        min_cleanup=Cleanup.DISPOSE,
        confined_to_guest_call_path=False,
    )
