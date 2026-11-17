"""Price retained Azure Luna 6 evidence and screen paired changes without new model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

METERS = {
    "input": "6-luna ShortCo Inp Std Gl 1M Tokens",
    "cached": "6-luna ShortCo Cd Inp Std Gl 1M Tokens",
    "cache_write": "6-luna ShortCo Cd Wr Std Gl 1M Tokens",
    "output": "6-luna ShortCo Opt Std Gl 1M Tokens",
}


def rate_card(retail: dict[str, Any]) -> dict[str, float]:
    """Require exactly the four USD, per-million Global Standard price meters."""
    result = {}
    for key, name in METERS.items():
        matches = [item for item in retail["Items"] if item["meterName"] == name]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one price for {name}")
        item = matches[0]
        if item["currencyCode"] != "USD" or item["unitOfMeasure"] != "1M":
            raise ValueError("Expected USD rates per million tokens")
        result[key] = float(item["retailPrice"])
    return result


def cost(usage: dict[str, Any], prices: dict[str, float]) -> float | None:
    """Charge writes at their total rate, excluding them from ordinary uncached input."""
    if any(usage[key] is None for key in METERS):
        return None
    ordinary = usage["input"] - usage["cached"] - usage["cache_write"]
    if ordinary < 0:
        raise ValueError("Reported cache reads and writes exceed input usage")
    return (
        ordinary * prices["input"]
        + sum(usage[key] * prices[key] for key in ("cached", "cache_write", "output"))
    ) / 1_000_000


def percent_change(before: float | None, after: float | None) -> float | None:
    """Return an undefined comparison when the baseline is absent or zero."""
    return (after / before - 1) * 100 if before and after is not None else None


def compression_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Associate each shrinking local pass with up to three later seeding requests."""
    events = []
    for step in row.get("compactions", []):
        if step["after"] >= step["before"]:
            continue
        following = [
            call for call in row["agent_seed_calls"] if call["call"] > step["agent_calls_before"]
        ][:3]
        events.append(
            {
                "before": step["before"],
                "after": step["after"],
                "reduction_percent": (1 - step["after"] / step["before"]) * 100,
                "remaining_headroom": step["budget"] - step["after"],
                "agent_calls_before": step["agent_calls_before"],
                "following_agent_hit_rates": [
                    call["usage"]["cached"] / call["usage"]["input"]
                    if call["usage"]["input"] and call["usage"]["cached"] is not None
                    else None
                    for call in following
                ],
            }
        )
    return events


def summarize(report: dict[str, Any], prices: dict[str, float]) -> dict[str, Any]:
    """Keep failed pairs visible and use token-weighted aggregate cache hit rates."""
    rows = report["conversations"]
    pairs = []
    for seed in sorted({row["seed"] for row in rows}):
        members = {row["variant"]: row for row in rows if row["seed"] == seed}
        if sum(row["seed"] == seed for row in rows) != len(members):
            raise ValueError("Duplicate pair members")
        if set(members) != {"baseline", "hardened"} or any(
            row["failed"] for row in members.values()
        ):
            pairs.append({"seed": seed, "complete": False})
            continue
        before, after = members["baseline"], members["hardened"]
        if before["scenario_sha256"] != after["scenario_sha256"]:
            raise ValueError("Paired workloads differ")
        if before["cache_id"] == after["cache_id"]:
            raise ValueError("Paired variants share a cache identity")
        priced = [cost(row["workload"], prices) for row in (before, after)]
        before_hit, after_hit = before["agent_seed"]["hit_rate"], after["agent_seed"]["hit_rate"]
        cache_delta = (
            (after_hit - before_hit) * 100
            if before_hit is not None and after_hit is not None
            else None
        )
        cost_delta = percent_change(*priced)
        pairs.append(
            {
                "seed": seed,
                "complete": True,
                "facts_left": [before["facts_left"], after["facts_left"]],
                "scoped_accuracy": [before["scoped_accuracy"], after["scoped_accuracy"]],
                "combined_accuracy": [before["combined_accuracy"], after["combined_accuracy"]],
                "workload_usd": priced,
                "cost_change_percent": cost_delta,
                "agent_cache_change_pp": cache_delta,
                "agent_uncached_change_percent": percent_change(
                    before["agent_seed"]["uncached"], after["agent_seed"]["uncached"]
                ),
                "within_cost_margin": cost_delta is not None and cost_delta <= 5,
                "within_cache_margin": cache_delta is not None and cache_delta >= -3,
                "no_retention_loss": after["facts_left"] >= before["facts_left"]
                and all(
                    bool(before[metric])
                    and bool(after[metric])
                    and min(after[metric]) >= min(before[metric])
                    for metric in ("combined_accuracy", "scoped_accuracy")
                ),
            }
        )
    aggregates = {}
    for variant in ("baseline", "hardened"):
        members = [row for row in rows if row["variant"] == variant]
        aggregate: dict[str, Any] = {
            "attempts": len(members),
            "failures": sum(row["failed"] for row in members),
        }
        if members and all(not row["failed"] and "workload" in row for row in members):
            for scope in ("agent_seed", "summarizer_seed", "workload"):
                usage: dict[str, Any] = {
                    key: sum(row[scope][key] for row in members)
                    if all(row[scope][key] is not None for row in members)
                    else None
                    for key in METERS
                }
                usage["usd"] = cost(usage, prices)
                usage["hit_rate"] = (
                    usage["cached"] / usage["input"]
                    if usage["input"] and usage["cached"] is not None
                    else None
                )
                aggregate[scope] = usage
        aggregates[variant] = aggregate
    starts = [
        {
            "seed": row["seed"],
            "variant": row["variant"],
            "first_cacheable_cached": next(
                (
                    call["usage"]["cached"]
                    for call in row.get("agent_seed_calls", [])
                    if call["usage"]["input"] is not None and call["usage"]["input"] >= 1024
                ),
                None,
            ),
        }
        for row in rows
    ]
    cold_starts_verified = bool(starts) and all(
        item["first_cacheable_cached"] == 0 for item in starts
    )
    measurements_complete = all(
        row.get("summarizer_failures", 0) == 0 and row.get("rate_limit_retries", 0) == 0
        for row in rows
    )
    return {
        "prices_usd_per_million": prices,
        "cost_formula": "((input - cached - cache_write) * input_rate + cached * cached_rate + cache_write * write_rate + output * output_rate) / 1000000",
        "cost_boundary": "Public retail estimate; conversation and summarizer usage, excluding recall probes. Not an invoice reconciliation.",
        "margins": {"max_cost_increase_percent": 5, "max_cache_loss_pp": 3},
        "pairs": pairs,
        "aggregate": aggregates,
        "compression": [
            {"seed": row["seed"], "variant": row["variant"], "events": compression_events(row)}
            for row in rows
        ],
        "cold_starts": starts,
        "cold_starts_verified": cold_starts_verified,
        "screen_within_margins": cold_starts_verified
        and measurements_complete
        and bool(pairs)
        and all(
            pair["complete"]
            and all(
                pair[key]
                for key in ("within_cost_margin", "within_cache_margin", "no_retention_loss")
            )
            for pair in pairs
        ),
        "statistical_equivalence_established": False,
    }


def main() -> None:
    """Create a separate assessment, preserving the original live evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("retail_prices", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path")
    report = json.loads(args.report.read_text(encoding="utf-8-sig"))
    retail = json.loads(args.retail_prices.read_text(encoding="utf-8-sig"))
    result = summarize(report, rate_card(retail))
    result["evidence_sha256"] = hashlib.sha256(args.report.read_bytes()).hexdigest()
    result["retail_price_rows"] = retail["Items"]
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
