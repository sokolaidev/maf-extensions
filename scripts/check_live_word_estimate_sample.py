"""Verify the Word fixture's actual cost artifacts, ordered stages and sandbox cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

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
                or not price["meterId"]
                or not price["effectiveStartDate"]
            ):
                return ["Wrong billing unit or missing meter attribution"]
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
        if "No complete inventory total is available." not in artifacts["estimate.md"].decode(
            "utf-8"
        ):
            return ["Markdown report omits the incomplete-total warning"]
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
