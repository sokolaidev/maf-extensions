"""Rendering and persistence of benchmark results.

Ratios that a provider cannot support are rendered as ``n/a`` rather than as zero. A
provider that reports no cache statistics is a different finding from a provider that
reports a zero hit rate, and collapsing the two would be the easiest way to draw a wrong
conclusion from this benchmark.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ._types import CellSummary, TurnRecord

__all__ = [
    "render_summary_table",
    "write_records_jsonl",
    "write_summary_csv",
]

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider", "provider"),
    ("model", "model"),
    ("size", "transcript"),
    ("strategy", "strategy"),
    ("turns", "turns"),
    ("sent_tok", "total_local_sent_tokens"),
    ("in_tok", "total_input_tokens"),
    ("cached", "total_cached_tokens"),
    ("hit%", "cache_hit_ratio"),
    ("reuse%", "local_reusable_ratio"),
    ("real%", "cache_realization"),
    ("breaks", "prefix_breaks"),
    ("no_in", "turns_missing_input"),
    ("p50_ms", "p50_latency_ms"),
    ("err", "errors"),
)

_PERCENT_FIELDS: frozenset[str] = frozenset(
    {"cache_hit_ratio", "local_reusable_ratio", "cache_realization"}
)


def _format_cell(field: str, value: Any) -> str:
    """Format one table cell for display."""
    if value is None:
        return "n/a"
    if field in _PERCENT_FIELDS:
        return f"{value * 100:.1f}"
    if field == "p50_latency_ms":
        return f"{value:.0f}"
    if isinstance(value, float):
        return f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _summary_row(summary: CellSummary, *, cache_read_ratio: float | None) -> list[str]:
    """Build one rendered row from a summary."""
    data = summary.to_dict(cache_read_ratio=cache_read_ratio)
    row = [_format_cell(field, data.get(field)) for _, field in _COLUMNS]
    if cache_read_ratio is not None:
        row.append(_format_cell("effective_input_tokens", data.get("effective_input_tokens")))
    return row


def render_summary_table(
    summaries: Sequence[CellSummary], *, cache_read_ratio: float | None = None
) -> str:
    """Render cell summaries as a fixed-width text table.

    Args:
        summaries: Cells to render, in the order they should appear.

    Keyword Args:
        cache_read_ratio: When provided, appends an effective-input-token column priced at
            that ratio for cached tokens.

    Returns:
        The rendered table, or a short notice when there is nothing to show.
    """
    if not summaries:
        return "No results."

    headers = [header for header, _ in _COLUMNS]
    if cache_read_ratio is not None:
        headers.append(f"eff_in@{cache_read_ratio:g}")

    rows = [_summary_row(summary, cache_read_ratio=cache_read_ratio) for summary in summaries]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def _line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()

    lines = [_line(headers), _line(["-" * width for width in widths])]
    lines.extend(_line(row) for row in rows)
    lines.append("")
    lines.append(
        "hit%   = provider cache reads as a share of reported input tokens\n"
        "reuse% = share of each prompt left byte-identical to the previous prompt (local oracle)\n"
        "real%  = hit% divided by reuse%: how much of the reusable prefix the provider "
        "actually served\n"
        "breaks = turns where the prompt was not a pure extension of the previous one\n"
        "no_in  = turns reporting cached tokens but no input count; hit% is suppressed when "
        "non-zero"
    )
    return "\n".join(lines)


def write_records_jsonl(path: Path, records: Iterable[TurnRecord]) -> int:
    """Write per-turn records as JSON Lines.

    Args:
        path: Destination file. Parent directories are created as needed.
        records: Records to write.

    Returns:
        The number of records written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False))
            handle.write("\n")
            written += 1
    return written


def write_summary_csv(
    path: Path, summaries: Sequence[CellSummary], *, cache_read_ratio: float | None = None
) -> int:
    """Write cell summaries as CSV.

    Args:
        path: Destination file. Parent directories are created as needed.
        summaries: Summaries to write.

    Keyword Args:
        cache_read_ratio: When provided, includes the effective-input-token column.

    Returns:
        The number of rows written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not summaries:
        path.write_text("", encoding="utf-8")
        return 0
    rows = [summary.to_dict(cache_read_ratio=cache_read_ratio) for summary in summaries]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
