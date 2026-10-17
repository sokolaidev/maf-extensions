"""Exact campaign control resources recorded before any Docker creation."""

import re
from typing import Any

from .evidence import require


def inventory(campaign: str) -> dict[str, Any]:
    """Derive bounded names and an ownership label from the trusted campaign identity."""
    require(re.fullmatch(r"[0-9a-f]{32}", campaign), "resource_campaign")
    return {
        "campaign": campaign,
        "label": "maf-sandbox.qualification=" + campaign,
        "names": {
            role: "oc-qualification-" + campaign + "-" + role
            for role in ("sentinel", "ownership", "bridge", "none")
        },
    }
