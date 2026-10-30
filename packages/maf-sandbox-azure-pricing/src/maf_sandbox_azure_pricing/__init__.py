"""Azure retail pricing through disposable, allowlisted sandboxes."""

import warnings as _warnings

from ._spec import AZURE_PRICING_TOOL_NAME, PriceType, azure_pricing_sandbox_spec
from ._tool import make_azure_pricing_tools

__all__ = [
    "AZURE_PRICING_TOOL_NAME",
    "MafSandboxAzurePricingExperimentalWarning",
    "PriceType",
    "azure_pricing_sandbox_spec",
    "make_azure_pricing_tools",
]


class MafSandboxAzurePricingExperimentalWarning(UserWarning):
    """Warning category for the experimental Azure pricing kind."""


try:
    _warnings.warn(
        "maf_sandbox_azure_pricing is experimental and may change or be removed without notice.",
        category=MafSandboxAzurePricingExperimentalWarning,
        stacklevel=2,
    )
except MafSandboxAzurePricingExperimentalWarning:
    # An informational warning must not block imports under -Werror.
    pass
