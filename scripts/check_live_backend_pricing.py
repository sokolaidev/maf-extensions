"""Validate host evidence from the experimental native Azure pricing sample."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

PREFIX = "[pricing-host] "
DIRECT_PRICE_TYPES = ("Consumption", "Reservation")


def require(condition: bool, message: str) -> None:
    """Reject incomplete evidence even under optimized Python."""
    if not condition:
        raise ValueError(message)


def pricing_outcome(value: dict[str, Any], requested: str | None, seconds: float) -> str:
    """Classify the kind result for one host-attributed query."""
    if "http_status" in value:
        require(
            type(value["http_status"]) is int and value == {"http_status": 429},
            "Invalid rate limit evidence",
        )
        return "rate_limited"
    if "deadline_seconds" in value:
        require(
            value == {"deadline_seconds": 90} and seconds >= 90,
            "Missing bounded execution deadline evidence",
        )
        return "deadline_exceeded"
    require(value.get("truncated") is False and bool(value.get("prices")), "Empty/truncated prices")
    for price in value["prices"]:
        amount = price.get("retailPrice")
        require(
            type(amount) in (int, float) and math.isfinite(amount) and amount >= 0,
            "Invalid retail amount",
        )
        require(
            price.get("currencyCode") == "USD" and bool(price.get("unitOfMeasure")),
            "Missing units or currency",
        )
        require(
            price.get("armSkuName") == "Standard_D4s_v5" and price.get("region") == "westeurope",
            "Wrong retail price identity",
        )
        category = price.get("priceType")
        require(
            category in ("Consumption", "Reservation", "DevTestConsumption"), "Wrong price type"
        )
        require(requested is None or requested == category, "Wrong price type for query")
        if category == "Reservation":
            require(bool(price.get("reservationTerm")), "Missing reservation term")
    return "priced"


def check(
    text: str, backend: str, *, model: bool | None = None, require_probes: bool = False
) -> dict[str, Any]:
    """Check the direct pricing outcomes and the lifecycle of every acquired sandbox."""
    records = [
        json.loads(line[len(PREFIX) :]) for line in text.splitlines() if line.startswith(PREFIX)
    ]
    identities = [row for row in records if row.get("stage") == "identity"]
    require(
        len(identities) == 1 and identities[0].get("backend") == backend, "Wrong backend identity"
    )
    identity = identities[0]
    require(
        bool(identity.get("source")) and bool(identity.get("runtime")),
        "Missing source/runtime identity",
    )
    versions = identity.get("versions", {})
    require(
        isinstance(versions, dict)
        and all(
            isinstance(versions.get(name), str) and versions[name]
            for name in ("maf-sandbox", "maf-sandbox-azure-pricing", "maf-sandbox-" + backend)
        ),
        "Missing installed versions",
    )
    sources = identity.get("source_sha256", {})
    require(
        isinstance(sources, dict)
        and all(
            isinstance(sources.get(name), str) and re.fullmatch("[0-9a-f]{64}", sources[name])
            for name in (
                "sample",
                "maf_sandbox",
                "maf_sandbox_azure_pricing",
                "maf_sandbox_" + backend.replace("-", "_"),
            )
        ),
        "Missing imported-source hashes",
    )
    if backend == "bubblewrap":
        digest = identity.get("proxy_sha256")
        require(isinstance(digest, str) and len(digest) == 64, "Missing proxy identity")
    else:
        require(bool(identity.get("cli")), "Missing sbx version")
    acquired = [row for row in records if row.get("stage") == "acquire"]
    calls = [row for row in records if row.get("stage") == "call"]
    reports = [row for row in records if row.get("stage") == "report"]
    disposed = [row for row in records if row.get("stage") == "disposed"]
    cleanups = [row for row in records if row.get("stage") == "cleanup"]
    require(len(cleanups) == 1 and records[-1] == cleanups[0], "Missing terminal cleanup")
    recorded_model = cleanups[0].get("model")
    require(
        type(recorded_model) is bool and (model is None or recorded_model is model),
        "Model mode differs from requested qualification",
    )
    require(
        cleanups[0].get("complete") is True and cleanups[0].get("calls") == len(calls),
        "Incomplete scope cleanup",
    )
    require(
        len(acquired) == len(calls) == len(reports) == len(disposed) >= (3 if model else 2),
        "Missing acquisition, report, timing or disposal",
    )
    require(len({row.get("instance") for row in acquired}) == len(acquired), "Instance reused")
    require(len({row.get("call") for row in calls}) == len(calls), "Call identity reused")
    if not recorded_model:
        require(len(calls) == len(DIRECT_PRICE_TYPES), "Expected exactly two direct pricing calls")
    outcomes = []
    for index, (acquire, call, report, dispose) in enumerate(
        zip(acquired, calls, reports, disposed, strict=True)
    ):
        require(
            bool(call.get("call")) and acquire.get("call") == call["call"] == report.get("call"),
            "Mismatched call attribution",
        )
        require(
            bool(acquire.get("instance"))
            and acquire["instance"] == dispose.get("instance")
            and dispose.get("complete") is True,
            "Incomplete instance disposal",
        )
        require(
            call.get("unclean") == 0 and call.get("failure") is None, "Call failed or was unclean"
        )
        for row in (call, report):
            seconds = row.get("seconds")
            require(
                type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0,
                "Invalid host timing",
            )
        requested = report.get("price_type")
        value = report.get("report")
        require(isinstance(value, dict), "Missing pricing outcome")
        if index < len(DIRECT_PRICE_TYPES):
            # Older price reports predate the explicit query field.
            if "price_type" not in report and "prices" in value:
                requested = DIRECT_PRICE_TYPES[index]
            require(requested == DIRECT_PRICE_TYPES[index], "Wrong direct price type")
        outcome = pricing_outcome(value, requested, min(call["seconds"], report["seconds"]))
        outcomes.append({"price_type": requested, "outcome": outcome})
    if require_probes:
        probes = [row for row in records if row.get("stage") == "probes"]
        require(len(probes) == 1, "Missing enforcement probes")
        results = probes[0].get("results", [])
        require(
            len(results) == 8
            and {row.get("name") for row in results}
            == {
                "method",
                "destination",
                "direct",
                "output",
                "timeout",
                "cancellation",
                "tool_timeout",
                "tool_cancellation",
            },
            "Missing enforcement or failure-cleanup case",
        )
        require(len({row.get("instance") for row in results}) == 8, "Probe identity reused")
        for row in results:
            seconds = row.get("seconds")
            require(
                row.get("disposed") is True
                and type(seconds) in (int, float)
                and math.isfinite(seconds)
                and seconds > 0,
                "Invalid probe cleanup or timing",
            )
            if row["name"] in ("method", "destination"):
                denial = row.get("denial", {})
                host = "prices.azure.com" if row["name"] == "method" else "example.com"
                method = "POST" if row["name"] == "method" else "GET"
                require(
                    denial.get("source") == backend
                    and denial.get("host") == host
                    and denial.get("method") == method,
                    "Missing host proxy denial identity",
                )
                record = denial.get("record", {})
                if backend == "bubblewrap":
                    audit = record.get("audit", {})
                    require(
                        record.get("rejected_by") == "allowlist"
                        and audit.get("action") == "reject"
                        and audit.get("status_code") == 403
                        and audit.get("host", "").split(":")[0] == host
                        and audit.get("method")
                        in ({method, "CONNECT"} if method == "GET" else {method}),
                        "Missing native proxy rejection",
                    )
                else:
                    sandbox = denial.get("sandbox", {})
                    require(
                        sandbox.get("id") == row["instance"]
                        and bool(sandbox.get("name"))
                        and record.get("sandbox_id") == sandbox["name"]
                        and record.get("host", "").split(":")[0] == host
                        and type(record.get("count")) is int
                        and record["count"] > 0,
                        "Missing sandbox proxy rejection",
                    )
            elif row["name"] == "tool_timeout":
                execution = row.get("execution", {})
                require(
                    execution.get("started") is True
                    and execution.get("expired") is True
                    and execution.get("timeout_seconds") == 0.001,
                    "Missing observed execution deadline",
                )
    return {"backend": backend, "calls": len(calls), "model": recorded_model, "outcomes": outcomes}


def main() -> None:
    """Check retained logs without model credentials or backend dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--backend", choices=("bubblewrap", "docker-sbx"), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--model", action="store_true", help="Also require an agent tool call")
    mode.add_argument("--no-model", action="store_false", dest="model")
    parser.set_defaults(model=None)
    parser.add_argument("--require-probes", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check(
                args.log.read_text("utf-8"),
                args.backend,
                model=args.model,
                require_probes=args.require_probes,
            )
        )
    )


if __name__ == "__main__":
    main()
