"""Validate the pinned CLI envelope, and report each retained row whole and its checked part.

The report carries every retained field of each numbered row. The checked prices repeat, under the
same numbers, only what cannot hold an instruction: numbers, flags, the kind's own selection
labels, and text the kind checks against a set or a strict format.
"""

import json
import math
import re
from collections.abc import Callable
from datetime import datetime
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
_CLOCK = r"(?:[01]\d|2[0-3]):[0-5]\d"
_DATE = re.compile(
    rf"\d{{4}}-\d{{2}}-\d{{2}}(?:T{_CLOCK}:[0-5]\d(?:\.\d{{1,7}})?(?:Z|[+-]{_CLOCK})?)?"
)
_CHECKED_SETS: dict[str, frozenset[str]] = {
    "unitOfMeasure": UNITS_OF_MEASURE,
    "region": PRICE_LIST_LOCATIONS,
    "reservationTerm": frozenset(_RESERVATION_TERMS),
}


def _is_currency(value: str) -> bool:
    return _CURRENCY.fullmatch(value) is not None


def _is_date(value: str) -> bool:
    # The shape admits only digits and separators and bounds the time and the offset, which
    # parsing may normalise (24:00 to the next day on Python 3.14, +05:60 to +06:00); parsing
    # then refuses an impossible calendar date.
    if _DATE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


_CHECKED_FORMATS: dict[str, Callable[[str], bool]] = {
    "currencyCode": _is_currency,
    "effectiveStartDate": _is_date,
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
    check = _CHECKED_FORMATS.get(name)
    return check is not None and isinstance(value, str) and check(value)


def checked_row(number: int, row: dict[str, Any]) -> dict[str, Any]:
    """The part of a projected row the kind can vouch for, carrying ``row: number``.

    A null and every field of :data:`_PRICE_FIELDS` are kept; a text field only when its check
    holds, so a unit outside the set leaves the checked row without a unit.
    """
    checked: dict[str, Any] = {"row": number}
    for name, value in row.items():
        if value is None or name in _PRICE_FIELDS or _checked(name, value):
            checked[name] = value
    return checked


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def parse_report(raw: bytes, max_rows: int, *, region: str | None = None) -> tuple[str, str, bool]:
    """Return the report, its checked prices, and whether more rows may exist.

    The report holds every retained field of the numbered rows, the counts and the selection the
    kind made. The checked prices repeat the rows' checked parts with the same counts and
    selection. A requested region the kind does not know is echoed in neither, so neither repeats
    the caller's text.
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
    shown = list(enumerate(projected[:max_rows], 1))
    summary = {
        "returned_rows": len(shown),
        "received_rows": len(rows),
        "truncated": truncated,
        "upstream_limit_reached": upstream_limit,
        **selection,
    }
    report = {"prices": [{"row": number, **row} for number, row in shown], **summary}
    checked = {"checked_prices": [checked_row(number, row) for number, row in shown], **summary}
    return _dumps(report), _dumps(checked), truncated
