"""Validate host evidence from the experimental native Azure pricing sample."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

PREFIX = "[pricing-host] "


def require(condition: bool, message: str) -> None:
    """Reject incomplete evidence even under optimized Python."""
    if not condition:
        raise ValueError(message)


def check(
    text: str, backend: str, *, model: bool = True, require_probes: bool = False
) -> dict[str, Any]:
    """Require two complete real price reports joined to fresh, disposed call identities."""
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
    require(bool(identity.get("versions")), "Missing installed versions")
    if backend == "bubblewrap":
        digest = identity.get("proxy_sha256")
        require(isinstance(digest, str) and len(digest) == 64, "Missing proxy identity")
    else:
        require(bool(identity.get("cli")), "Missing sbx version")
    acquired = [row for row in records if row.get("stage") == "acquire"]
    calls = [row for row in records if row.get("stage") == "call"]
    reports = [row for row in records if row.get("stage") == "report"]
    disposed = [row for row in records if row.get("stage") == "disposed"]
    require(
        len(acquired) == len(calls) == len(reports) == len(disposed) >= (3 if model else 2),
        "Missing acquisition, report, timing or disposal",
    )
    require(len({row.get("instance") for row in acquired}) == len(acquired), "Instance reused")
    require(len({row.get("call") for row in calls}) == len(calls), "Call identity reused")
    categories: set[str] = set()
    for acquire, call, report, dispose in zip(acquired, calls, reports, disposed, strict=True):
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
        value = report.get("report", {})
        require(
            value.get("truncated") is False and bool(value.get("prices")), "Empty/truncated prices"
        )
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
                price.get("armSkuName") == "Standard_D4s_v5"
                and price.get("region") == "westeurope",
                "Wrong retail price identity",
            )
            category = price.get("priceType")
            require(category in ("Consumption", "Reservation"), "Wrong price type")
            categories.add(category)
            if category == "Reservation":
                require(bool(price.get("reservationTerm")), "Missing reservation term")
    require(
        categories == {"Consumption", "Reservation"}, "Missing consumption/reservation coverage"
    )
    cleanups = [row for row in records if row.get("stage") == "cleanup"]
    require(len(cleanups) == 1 and records[-1] == cleanups[0], "Missing terminal cleanup")
    require(
        cleanups[0].get("complete") is True and cleanups[0].get("calls") == len(calls),
        "Incomplete scope cleanup",
    )
    require(cleanups[0].get("model") is model, "Model mode differs from requested qualification")
    if require_probes:
        probes = [row for row in records if row.get("stage") == "probes"]
        require(len(probes) == 1, "Missing enforcement probes")
        results = probes[0].get("results", [])
        require(
            len(results) == 6
            and {row.get("name") for row in results}
            == {
                "method",
                "destination",
                "direct",
                "output",
                "timeout",
                "cancellation",
            },
            "Missing enforcement or failure-cleanup case",
        )
        require(len({row.get("instance") for row in results}) == 6, "Probe identity reused")
        for row in results:
            seconds = row.get("seconds")
            require(
                row.get("disposed") is True
                and type(seconds) in (int, float)
                and math.isfinite(seconds)
                and seconds > 0,
                "Invalid probe cleanup or timing",
            )
    return {"backend": backend, "calls": len(calls), "model": model}


def main() -> None:
    """Check retained logs without model credentials or backend dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--backend", choices=("bubblewrap", "docker-sbx"), required=True)
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--require-probes", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check(
                args.log.read_text("utf-8"),
                args.backend,
                model=not args.no_model,
                require_probes=args.require_probes,
            )
        )
    )


if __name__ == "__main__":
    main()
