"""Select replacement scan routes independently of optional diagnostic builds."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bicep_source import verify_inventory as verify_bicep_inventory
from select_image_security import PROFILES

BICEP_ROUTES = {"bicep": "source", "bicep-avm": "source", "bicep-docker-sbx": "source"}


def scan_route(profile: str, requested: str) -> str:
    """Use the reviewed replacement policy unless a diagnostic route is selected."""
    if profile not in PROFILES or requested not in {"policy", "source", "upstream"}:
        raise ValueError("Unknown image scan profile or route")
    if profile not in BICEP_ROUTES:
        return "upstream"
    return BICEP_ROUTES[profile] if requested == "policy" else requested


def evidence_route(
    profile: str, image_id: str, runtime: dict[str, Any], sbom: dict[str, Any]
) -> str:
    """Require a compiler receipt and inventory belonging to the scanned image."""
    if runtime.get("profile") != profile or runtime.get("imageId") != image_id:
        raise ValueError("Bicep runtime evidence identifies another image or profile")
    build = runtime.get("bicepAvm", runtime.get("bicep", {})).get("build", {})
    route = build.get("route")
    if route not in {"source", "upstream"}:
        raise ValueError("Bicep runtime evidence has no valid route")
    verify_bicep_inventory(sbom, runtime)
    return route


def main() -> None:
    """Expose the selected route and its purpose to the scan job."""
    profile = os.environ["PROFILE"]
    route = scan_route(profile, os.environ.get("REQUESTED_BICEP_ROUTE", "policy"))
    purpose = (
        "diagnostic"
        if profile in BICEP_ROUTES and route != BICEP_ROUTES[profile]
        else "replacement"
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(f"bicep_route={route}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(f"Scan purpose: **{purpose}**; Bicep acquisition route: `{route}`.\n")


if __name__ == "__main__":
    main()
