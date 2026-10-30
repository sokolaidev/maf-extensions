"""Fixed public pricing source and disposable execution boundary."""

from enum import StrEnum

from maf_sandbox import (
    Capability,
    Cleanup,
    Egress,
    EgressRule,
    HttpMethod,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxSpec,
)

AZURE_PRICING_TOOL_NAME = "azure_pricing_get"


class PriceType(StrEnum):
    """Retail price categories; savings plans are requested separately."""

    CONSUMPTION = "Consumption"
    RESERVATION = "Reservation"
    DEV_TEST_CONSUMPTION = "DevTestConsumption"


def azure_pricing_sandbox_spec(
    image: str | None = None, image_id: str | None = None
) -> SandboxSpec:
    """Require a fresh POSIX sandbox allowing only GETs to the public pricing host."""
    return SandboxSpec(
        kind="azure-pricing",
        image=image,
        image_id=image_id,
        work_dir=None,
        requires=frozenset({Capability.EXEC}),
        requires_os_family=OsFamily.POSIX,
        min_isolation=Isolation.CONTAINER,
        isolation_scope=IsolationScope.CALL,
        min_cleanup=Cleanup.DISPOSE,
        confined_to_guest_call_path=False,
        egress=Egress.ALLOWLIST,
        egress_allow=(EgressRule("prices.azure.com", methods=(HttpMethod.GET,)),),
    )
