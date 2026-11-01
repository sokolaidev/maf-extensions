"""Verify the Word fixture's actual cost artifacts, ordered stages and sandbox cleanup."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from live_sample_evidence import duration, records

# Independent identities keep sample-catalog regressions visible to the live gate.
FIXTURE_METERS = {
    "linux-b2s": ("Virtual Machines", "Virtual Machines BS Series", "B2s", "B2s"),
    "linux-d2s-v5": (
        "Virtual Machines",
        "Virtual Machines Dsv5 Series",
        "Standard_D2s_v5",
        "D2s v5",
    ),
    "premium-ssd-p10": ("Storage", "Premium SSD Managed Disks", "P10 LRS", "P10 LRS Disk"),
}


def markdown_matches(report: dict[str, Any], rendered: str) -> bool:
    """Compare the displayed table and summary with validated JSON, without the renderer."""

    def flat(value: object) -> str:
        return str(value).replace("\n", " ").replace("\r", " ")

    def cells(line: str) -> list[str]:
        if not line.startswith("| ") or not line.endswith(" |"):
            raise ValueError("Malformed Markdown row")
        values = line[2:-2].split(" | ")
        if len(values) != 7:
            raise ValueError("Wrong Markdown column count")
        return [html.unescape(re.sub(r"\\([\\|`\[\]*_])", r"\1", value)) for value in values]

    lines = rendered.splitlines()
    table = [line for line in lines if line.startswith("|")]
    if len(table) != len(report["rows"]) + 2:
        return False
    if cells(table[0]) != [
        "Resource",
        "Product",
        "Qty",
        "Unit price",
        "Usage/resource/month",
        "Monthly cost",
        "Status / assumptions",
    ] or not re.fullmatch(r"\|(?:[-:]+\|){7}", table[1]):
        return False
    for line, row in zip(table[2:], report["rows"], strict=True):
        name, product, quantity, unit_price, usage, cost, notes = cells(line)
        expected_notes = "; ".join(
            flat(value) for value in (row["status"], row.get("assumption"), row["notes"]) if value
        )
        if (name, product, quantity, notes) != (
            flat(row["name"]),
            row["product_key"],
            str(row["quantity"]) if row["quantity"] is not None else "?",
            expected_notes,
        ):
            return False
        if row["price"] is None:
            if (unit_price, usage, cost) != ("—", "—", "—"):
                return False
        else:
            amount, separator, unit = unit_price.partition(" / ")
            if (
                not separator
                or unit != row["price"]["unitOfMeasure"]
                or Decimal(amount) != Decimal(str(row["price"]["retailPrice"]))
                or Decimal(usage) != Decimal(row["usage_per_resource"])
                or Decimal(cost) != Decimal(row["monthly_cost"])
            ):
                return False
    subtotal_lines = [line for line in lines if line.startswith("Estimated subtotal:")]
    if len(subtotal_lines) != 1:
        return False
    subtotal = re.fullmatch(
        r"Estimated subtotal: \*\*([0-9]+(?:\.[0-9]+)?) USD\*\*\.", subtotal_lines[0]
    )
    if subtotal is None or Decimal(subtotal[1]) != Decimal(report["estimated_subtotal"]):
        return False
    return [line for line in lines if line.startswith("Region:")] == [
        f"Region: {report['region']}. Currency: {report['currency']}. Retrieved: {flat(report['queried_at_utc'])}."
    ] and [line for line in lines if line.startswith("Unresolved resources:")] == [
        f"Unresolved resources: **{report['unresolved_resources']}**. No complete inventory total is available."
    ]


def assess(
    output: str, artifacts: dict[str, bytes], *, inventory_source: str = "model"
) -> list[str]:
    """Require live host evidence and recalculate the known fixture's three priced rows."""
    try:
        measured = records(output)
        stages = [item["stage"] for item in measured]
        expected = ["tool_call", "scan", "tool_call", "inventory"] + ["tool_call"] * 3
        expected += ["cleanup", "artifact", "artifact", "report"]
        if stages != expected:
            return ["Missing, extra or out-of-order pipeline stages"]
        calls = [item for item in measured if item["stage"] == "tool_call"]
        if any(
            item.get("complete") is not True or not duration(item.get("seconds")) for item in calls
        ):
            return ["Tool execution or disposal was incomplete"]
        if len({item.get("call") for item in calls}) != 5 or any(
            not item.get("call") for item in calls
        ):
            return ["Tool call identities are missing or duplicated"]
        if measured[1] != {"stage": "scan", "clean": True, "byte_bound": True}:
            return ["No byte-bound clean scan"]
        if measured[3] != {"stage": "inventory", "resources": 4}:
            return ["Fixture inventory is incomplete"]
        if measured[7] != {"stage": "cleanup", "complete": True, "calls": 5}:
            return ["Final cleanup was not confirmed"]
        if measured[-1] != {"stage": "report", "unresolved": 1, "files": 2}:
            return ["Expected three priced rows and one unresolved row"]
        if set(artifacts) != {"estimate.json", "estimate.md"}:
            return ["Both cost artifacts are required"]
        for item, name in zip(measured[8:10], ("estimate.json", "estimate.md"), strict=True):
            payload = artifacts[name]
            if not payload or item != {
                "stage": "artifact",
                "name": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }:
                return ["Retained report does not match host read-back"]
        report = json.loads(artifacts["estimate.json"])
        if report["inventory_source"] != inventory_source:
            return ["Inventory source does not match the verification mode"]
        if report["currency"] != "USD" or report["region"] != "westeurope":
            return ["Unexpected fixture region or currency"]
        retrieved = datetime.fromisoformat(report["queried_at_utc"])
        if retrieved.utcoffset() is None:
            return ["Retrieval time must be timezone-aware"]
        rows = report["rows"]
        expected_rows = {
            "linux-b2s": (2, "730.0", "1 Hour"),
            "linux-d2s-v5": (1, "160.0", "1 Hour"),
            "premium-ssd-p10": (2, "1", "1/Month"),
        }
        if len(rows) != 4 or {row["product_key"] for row in rows} != {*expected_rows, "unknown"}:
            return ["Fixture resources were omitted, duplicated or misidentified"]
        total = Decimal(0)
        for row in rows:
            if not isinstance(row["evidence"], str) or not row["evidence"].strip():
                return ["Missing extracted document evidence"]
            if row["product_key"] == "unknown":
                if row["monthly_cost"] is not None or row["price"] is not None:
                    return ["Unsupported storage was assigned a price"]
                continue
            quantity, usage, unit = expected_rows[row["product_key"]]
            price = row["price"]
            identity = tuple(
                price[field] for field in ("serviceName", "productName", "skuName", "meterName")
            )
            if identity != FIXTURE_METERS[row["product_key"]]:
                return ["Selected meter does not match the fixture product"]
            if row["quantity"] != quantity or Decimal(row["usage_per_resource"]) != Decimal(usage):
                return ["Fixture quantity or usage differs"]
            if (
                price["unitOfMeasure"] != unit
                or price["priceType"] != "Consumption"
                or price["currencyCode"] != "USD"
                or price["region"] != "westeurope"
                or price.get("isPrimaryMeterRegion") is not True
                or price.get("reservationTerm")
                or not isinstance(price["meterId"], str)
                or not price["meterId"]
            ):
                return ["Wrong billing terms or missing primary-meter attribution"]
            effective = datetime.fromisoformat(price["effectiveStartDate"])
            if effective.utcoffset() is None or effective > retrieved:
                return ["Meter effective date is not current at retrieval"]
            if isinstance(price["retailPrice"], bool) or not isinstance(
                price["retailPrice"], (int, float)
            ):
                return ["Fixture retail price must be numeric"]
            retail = Decimal(str(price["retailPrice"]))
            if not retail.is_finite() or retail <= 0:
                return ["Fixture retail price must be positive"]
            cost = (retail * quantity * Decimal(usage)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if Decimal(row["monthly_cost"]) != cost:
                return ["Cost arithmetic differs from the selected retail price"]
            total += cost
        if report["unresolved_resources"] != 1 or report["inventory_total"] is not None:
            return ["Partial estimate was presented as complete"]
        if Decimal(report["estimated_subtotal"]) != total:
            return ["Subtotal differs from the priced rows"]
        if not markdown_matches(report, artifacts["estimate.md"].decode("utf-8")):
            return ["Markdown cost table or summary differs from the JSON report"]
    except (KeyError, TypeError, ValueError, InvalidOperation, UnicodeError):
        return ["Malformed cost report or measurement"]
    return []


def main() -> int:
    """Check a saved log and report directory, with model extraction required by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inventory-source", choices=("model", "fixture"), default="model")
    args = parser.parse_args()
    artifacts = {
        name: (args.output / name).read_bytes() for name in ("estimate.json", "estimate.md")
    }
    failures = assess(
        args.log.read_text("utf-8"), artifacts, inventory_source=args.inventory_source
    )
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
