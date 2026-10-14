"""Check Gateway-projected compiler evidence independently of assistant prose."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

INVALID = "output greeting int = 'hello'"
VALID = "output greeting string = 'hello'"
TOOL = "bicep__bicep_validate"


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(source: str) -> str:
    return hashlib.sha256(
        json.dumps([["main.bicep", source]], separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def check_turn(
    records: list[dict[str, Any]], turn: str, *, repaired: bool, image: str, config_hash: str
) -> dict[str, Any]:
    selected = [record for record in records if record["turn"] == turn]
    require(bool(selected), "No observed Bicep tool result for this turn")
    for record in selected:
        files = record["arguments"].get("files")
        require(isinstance(files, list) and len(files) == 1, "Expected one inline source")
        require(files[0].get("path") == "main.bicep", "Unexpected source path")
        source = files[0].get("content")
        require(isinstance(source, str), "Missing submitted source")
        projected = record["projected"]
        require(
            projected.get("tool") == {"id": "mcp:bicep:" + TOOL, "name": TOOL, "source": "mcp"},
            "Result belongs to another tool",
        )
        result = projected["result"]
        require(not result.get("isError", False), "Tool returned an error")
        require(len(result["content"]) == 1, "Expected one projected result item")
        structured = result["details"]["structuredContent"]
        require(structured.get("cleanup") == "confirmed", "Cleanup was not confirmed")
        require(structured.get("image") == image, "Wrong compiler image")
        require(structured.get("config_sha256") == config_hash, "Wrong compiler configuration")
        require(structured.get("source_sha256") == digest(source), "Result/source mismatch")
        require(structured.get("completed") is True, "Compiler did not complete")
        require(structured.get("status") == "ok", "Compiler result is not authoritative")
        if not repaired:
            require(source == INVALID, "First turn changed the supplied source")
            require(structured.get("verdict") == "invalid", "Expected an invalid first verdict")
    last = selected[-1]
    source = last["arguments"]["files"][0]["content"]
    result = last["projected"]["result"]["details"]["structuredContent"]
    if repaired:
        require(result["verdict"] == "valid", "Last repair did not validate")
        require(
            re.fullmatch(r"\s*output\s+greeting\s+string\s*=\s*'hello'\s*", source),
            "Expected the minimal greeting-preserving repair",
        )
        require(digest(source) != digest(INVALID), "Repair did not change the source")
    return {
        "calls": len(selected),
        **{
            key: result[key]
            for key in (
                "completed",
                "verdict",
                "cleanup",
                "source_sha256",
                "config_sha256",
                "image",
            )
        },
    }
