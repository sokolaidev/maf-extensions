"""Validate the pinned CLI envelope, and split each retained row into its price and description.

The price carries only what cannot hold an instruction: numbers, flags, the kind's own selection
labels, and text the kind checks against a set or a strict format. Everything else the API wrote
about a row is its description, joined to the price by the row's number.
"""

import json
import math
import re
from typing import Any, cast

from ._regions import PRICE_LIST_LOCATIONS, select_region
from ._spec import PriceType
from ._units import UNITS_OF_MEASURE

UPSTREAM_ROW_LIMIT = 5000
_PRICE_FIELDS = frozenset(
    {
        "retailPrice",
        "unitPrice",
        "isPrimaryMeterRegion",
        "priceType",
        "savingsPlan",
        "matched_by",
        "zone",
        "zone_basis",
        "zone_reference_region",
        "zone_family",
    }
)
_RESERVATION_TERMS = ("1 Year", "3 Years")
_CURRENCY = re.compile("[A-Z]{3}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})?)?")
_CHECKED_SETS: dict[str, frozenset[str]] = {
    "unitOfMeasure": UNITS_OF_MEASURE,
    "region": PRICE_LIST_LOCATIONS,
    "reservationTerm": frozenset(_RESERVATION_TERMS),
}
_CHECKED_FORMATS: dict[str, re.Pattern[str]] = {
    "currencyCode": _CURRENCY,
    "effectiveStartDate": _DATE,
}
_TEXT_FIELDS = (
    "armSkuName",
    "skuName",
    "productName",
    "serviceName",
    "serviceFamily",
    "region",
    "location",
    "currencyCode",
    "unitOfMeasure",
    "priceType",
    "reservationTerm",
    "effectiveStartDate",
    "meterName",
    "meterId",
)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _price(value: object) -> int | float:
    if type(value) not in (int, float) or not isinstance(value, (int, float)):
        raise ValueError("Invalid price")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid price")
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast("dict[str, Any]", value)


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return cast("list[Any]", value)


def _row(raw: object) -> dict[str, Any]:
    value = _object(raw)
    row: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        if name not in value:
            continue
        text = value[name]
        if text is not None and (
            not isinstance(text, str) or len(text) > 2048 or any(ord(c) < 32 for c in text)
        ):
            raise ValueError("Invalid price text")
        row[name] = text
    for name in ("currencyCode", "unitOfMeasure", "priceType"):
        if not row.get(name):
            raise ValueError("Missing price units or category")
    PriceType(row["priceType"])
    if (
        row["priceType"] == PriceType.RESERVATION
        and row.get("reservationTerm") not in _RESERVATION_TERMS
    ):
        raise ValueError("Missing reservation term")
    for name in ("retailPrice", "unitPrice"):
        row[name] = _price(value.get(name))
    if "isPrimaryMeterRegion" in value:
        if type(value["isPrimaryMeterRegion"]) is not bool:
            raise ValueError("Invalid primary meter flag")
        row["isPrimaryMeterRegion"] = value["isPrimaryMeterRegion"]
    if value.get("savingsPlan") is not None:
        plans = _array(value["savingsPlan"])
        if len(plans) > 2:
            raise ValueError("Invalid savings plans")
        selected: list[dict[str, Any]] = []
        for raw_plan in plans:
            plan = _object(raw_plan)
            if plan.get("term") not in _RESERVATION_TERMS:
                raise ValueError("Invalid savings plan term")
            selected.append(
                {
                    "term": plan["term"],
                    "retailPrice": _price(plan.get("retailPrice")),
                    "unitPrice": _price(plan.get("unitPrice")),
                }
            )
        row["savingsPlan"] = selected
    return row


def _checked(name: str, value: object) -> bool:
    if (allowed := _CHECKED_SETS.get(name)) is not None:
        return value in allowed
    pattern = _CHECKED_FORMATS.get(name)
    return pattern is not None and isinstance(value, str) and pattern.fullmatch(value) is not None


def split_row(number: int, row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a projected row into its price and its description, each carrying ``row: number``.

    A null and every field of :data:`_PRICE_FIELDS` go to the price; a text field goes there only
    when its check holds, so a unit outside the set leaves the row's prices without a unit.
    """
    price: dict[str, Any] = {"row": number}
    description: dict[str, Any] = {"row": number}
    for name, value in row.items():
        if value is None or name in _PRICE_FIELDS or _checked(name, value):
            price[name] = value
        else:
            description[name] = value
    return price, description


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def parse_report(raw: bytes, max_rows: int, *, region: str | None = None) -> tuple[str, str, bool]:
    """Return the price report, the row descriptions, and whether more rows may exist.

    The report holds the numbered prices, the counts and the selection the kind made; the
    descriptions hold only what else the API wrote about the same numbered rows. A requested
    region the kind does not know is echoed nowhere, so neither part repeats the caller's text.
    """
    envelope = _object(
        json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    )
    if type(envelope.get("status")) is not int:
        raise ValueError("Invalid CLI envelope")
    results = _object(envelope.get("results"))
    if envelope["status"] != 200:
        raise ValueError("Pricing command failed")
    rows = _array(results.get("prices"))
    if len(rows) > UPSTREAM_ROW_LIMIT:
        raise ValueError("Invalid price collection")
    # Validate omitted rows too: a cap must not turn a malformed report into a success.
    projected = [_row(row) for row in rows]
    selection: dict[str, Any] = {}
    if region is not None:
        projected, notes = select_region(projected, region)
        if region in PRICE_LIST_LOCATIONS:
            selection["requested_region"] = region
        selection |= {"selected_rows": len(projected), "selection_notes": notes}
    upstream_limit = len(rows) == UPSTREAM_ROW_LIMIT
    truncated = len(projected) > max_rows or upstream_limit
    split = [split_row(number, row) for number, row in enumerate(projected[:max_rows], 1)]
    report = {
        "prices": [price for price, _ in split],
        "returned_rows": len(split),
        "received_rows": len(rows),
        "truncated": truncated,
        "upstream_limit_reached": upstream_limit,
        **selection,
    }
    descriptions = {"descriptions": [description for _, description in split]}
    return _dumps(report), _dumps(descriptions), truncated
