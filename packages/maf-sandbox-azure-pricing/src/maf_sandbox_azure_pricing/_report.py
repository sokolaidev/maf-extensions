"""Validate the pinned CLI envelope and retain only bounded pricing fields."""

import json
import math
from typing import Any, cast

from ._spec import PriceType

UPSTREAM_ROW_LIMIT = 5000
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
    if row["priceType"] == PriceType.RESERVATION and row.get("reservationTerm") not in (
        "1 Year",
        "3 Years",
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
            if plan.get("term") not in ("1 Year", "3 Years"):
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


def parse_report(raw: bytes, max_rows: int) -> tuple[str, bool]:
    """Return projected JSON and a conservative indication that more rows may exist."""
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
    upstream_limit = len(rows) == UPSTREAM_ROW_LIMIT
    truncated = len(rows) > max_rows or upstream_limit
    return json.dumps(
        {
            "prices": projected[:max_rows],
            "returned_rows": min(len(rows), max_rows),
            "received_rows": len(rows),
            "truncated": truncated,
            "upstream_limit_reached": upstream_limit,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ), truncated
