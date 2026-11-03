"""Host-side billing-zone selection; source tables are documented in the package guide."""

import re
from typing import Any

BILLING_ZONES = tuple(f"Zone {number}" for number in range(1, 10)) + (
    "US Gov Zone 1",
    "US Gov Zone 2",
)
_ODATA_TOKEN = re.compile(r"'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9]*")

_NETWORK_REGIONS = {
    "Zone 1": (
        "australiacentral australiacentral2 canadacentral canadaeast centralus eastus eastus2 "
        "francecentral francesouth germanynorth germanywestcentral northcentralus northeurope "
        "norwayeast norwaywest southcentralus switzerlandnorth switzerlandwest uksouth ukwest "
        "westcentralus westeurope westus westus2"
    ).split(),
    "Zone 2": (
        "australiaeast australiasoutheast centralindia eastasia japaneast japanwest koreacentral "
        "koreasouth southeastasia southindia westindia"
    ).split(),
    "Zone 3": "brazilsouth southafricanorth southafricawest uaecentral uaenorth".split(),
    "US Gov Zone 1": "usgovarizona usgovtexas usgovvirginia".split(),
}
_NETWORK_ZONE = {region: zone for zone, regions in _NETWORK_REGIONS.items() for region in regions}

# Geographic proximity is an estimate, not a published billing assignment.
_NEAREST_NETWORK_REGION = {
    "austriaeast": "germanynorth",
    "belgiumcentral": "westeurope",
    "brazilsoutheast": "brazilsouth",
    "chilecentral": "brazilsouth",
    "denmarkeast": "germanynorth",
    "indiasouthcentral": "centralindia",
    "indonesiacentral": "southeastasia",
    "israelcentral": "uaecentral",
    "italynorth": "switzerlandnorth",
    "malaysiawest": "southeastasia",
    "mexicocentral": "southcentralus",
    "newzealandnorth": "australiaeast",
    "polandcentral": "germanynorth",
    "qatarcentral": "uaecentral",
    "southeastasia3": "southeastasia",
    "spaincentral": "francesouth",
    "swedencentral": "norwayeast",
    "swedensouth": "germanynorth",
    "taiwannorth": "eastasia",
    "taiwannorthwest": "eastasia",
    "westus3": "westus",
}
_EDGE_REGIONS = {
    1: (
        "canadacentral canadaeast centralus eastus eastus2 mexicocentral northcentralus "
        "southcentralus westcentralus westus westus2 westus3"
    ).split(),
    2: (
        "eastasia indonesiacentral japaneast japanwest malaysiawest newzealandnorth "
        "southeastasia southeastasia3 taiwannorth taiwannorthwest"
    ).split(),
    3: "brazilsouth brazilsoutheast chilecentral".split(),
    4: "australiacentral australiacentral2 australiaeast australiasoutheast".split(),
    5: "centralindia indiasouthcentral southindia westindia".split(),
    6: (
        "austriaeast belgiumcentral denmarkeast francecentral francesouth germanynorth "
        "germanywestcentral italynorth northeurope norwayeast norwaywest polandcentral "
        "spaincentral swedencentral swedensouth switzerlandnorth switzerlandwest uksouth "
        "ukwest westeurope"
    ).split(),
    7: "israelcentral qatarcentral southafricanorth southafricawest uaecentral uaenorth".split(),
    8: "koreacentral koreasouth".split(),
    9: "usgovarizona usgovtexas usgovvirginia".split(),
}
_EDGE_ZONE = {region: zone for zone, regions in _EDGE_REGIONS.items() for region in regions}


def expanded_region(region: str | None, predicate: str | None) -> str | None:
    """Expand ARM regions unless the caller explicitly addresses price-list locations."""
    if region is None or region == "Global" or region in BILLING_ZONES:
        return None
    if predicate and any(
        token.casefold() == "armregionname" for token in _ODATA_TOKEN.findall(predicate)
    ):
        return None
    return region


def region_predicate(region: str) -> str:
    """Quote the region as an OData literal and group all candidate locations."""
    locations = (region, "Global", *BILLING_ZONES)
    return (
        "("
        + " or ".join(
            "armRegionName eq '" + location.replace("'", "''") + "'" for location in locations
        )
        + ")"
    )


def _family(row: dict[str, Any]) -> str | None:
    service, product = row.get("serviceName"), row.get("productName")
    if service == "ExpressRoute":
        return "expressroute"
    if service == "Azure DNS":
        return "dns"
    if service == "Azure Front Door Service" and product == "Azure Front Door":
        return "front_door"
    if service == "Content Delivery Network" or (
        service == "Azure Front Door Service" and product == "Azure Front Door Service"
    ):
        return "cdn_classic"
    if (service, product) in (
        ("Virtual Network", "Virtual Network Manager"),
        ("Virtual Network", "Virtual Network"),
        ("VPN Gateway", "VPN Gateway Bandwidth"),
        ("Azure Cosmos DB", "Azure Cosmos DB Data Transfer"),
    ) or service == "Azure Multicloud Interconnect":
        return "network"
    return None


def _zone(region: str, family: str | None) -> dict[str, str] | None:
    if family == "network":
        reference = _NEAREST_NETWORK_REGION.get(region, region)
        zone = _NETWORK_ZONE.get(reference)
        if zone:
            return {
                "zone": zone,
                "zone_basis": "published_region" if reference == region else "nearest_region",
                "zone_reference_region": reference,
            }
    edge = _EDGE_ZONE.get(region)
    if family == "front_door" and edge:
        return {"zone": f"Zone {edge}", "zone_basis": "geography"}
    if family == "cdn_classic" and edge and edge != 9:
        zone = {6: 1, 7: 1, 8: 2}.get(edge, edge)
        return {"zone": f"Zone {zone}", "zone_basis": "geography"}
    if family == "dns" and edge:
        return {
            "zone": "US Gov Zone 1" if edge == 9 else "Zone 1",
            "zone_basis": "published_region" if edge == 9 else "uniform_commercial",
        }
    return None


def _meter(row: dict[str, Any]) -> tuple[str, str, str] | None:
    service = row.get("serviceName")
    product = row.get("productName")
    meter = row.get("meterName")
    if isinstance(service, str) and isinstance(product, str) and isinstance(meter, str):
        if not (service and product and meter):
            return None
        return service, product, meter
    return None


def select_region(
    rows: list[dict[str, Any]], region: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Retain regional and selected-zone rows, then Global fallbacks per complete meter key."""
    selected: list[dict[str, Any]] = []
    notes: set[str] = set()
    if region not in _EDGE_ZONE:
        notes.add(
            "Unknown region: billing zones were not selected; use an explicit armRegionName filter."
        )
    for row in rows:
        location = row.get("region")
        if location == region:
            selected.append(row | {"matched_by": "region"})
        elif location in BILLING_ZONES:
            family = _family(row)
            if family == "expressroute":
                notes.add(
                    "ExpressRoute zone rows omitted: "
                    "select the circuit peering location explicitly."
                )
                continue
            if family is None:
                notes.add(
                    "Unrecognized service/product zone rows omitted; "
                    "use an explicit armRegionName filter."
                )
                continue
            match = _zone(region, family)
            if match is None:
                notes.add(
                    "No billing-zone mapping for a returned family; "
                    "use an explicit armRegionName filter."
                )
            elif location == match["zone"]:
                selected.append(row | {"matched_by": "zone", "zone_family": family} | match)
                if match["zone_basis"] == "nearest_region":
                    notes.add(
                        "Network zone is estimated from the nearest listed region, "
                        "not a published assignment for the requested region."
                    )
                if family in ("front_door", "cdn_classic"):
                    notes.add(
                        "Edge zone uses the requested geography; actual CDN/Front Door billing "
                        "follows the serving edge, not the origin."
                    )
    covered = {key for row in selected if (key := _meter(row)) is not None}
    selected.extend(
        row | {"matched_by": "global"}
        for row in rows
        if row.get("region") == "Global" and (_meter(row) is None or _meter(row) not in covered)
    )
    return selected, sorted(notes)
