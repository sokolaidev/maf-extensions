"""Bind each Gateway outcome to a distinct request and provider observation."""

from __future__ import annotations

import importlib
import re
from typing import Any

from .evidence import decode, encode, require


def verify(value: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Correlate all six turns independently of their summarized result rows."""
    checker = importlib.import_module("openclaw_gateway_http_check")
    provider = value["provider_observations"]
    require(isinstance(provider, list) and len(provider) == 12, "gateway_observation_inventory")
    requests = []
    turns: set[str] = set()
    for session, outcomes in zip(value["session_ids"], value["sessions"], strict=True):
        for case in ("valid", "invalid", "incomplete"):
            row = outcomes[case]
            turn = row.get("turn")
            require(
                row.get("session") == session
                and isinstance(turn, str)
                and re.fullmatch(r"[0-9a-f]{32}", turn)
                and turn not in turns,
                "gateway_turn_identity",
            )
            turns.add(turn)
            requests.append({"session": session, "turn": turn, "scenario": case})
            observed = [r for r in provider if r.get("turn") == turn]
            require(
                len(observed) == 2
                and all(r.get("session") == session and r.get("scenario") == case for r in observed)
                and sum("tool_result" in r for r in observed) == 1,
                "gateway_turn_observations",
            )
            projected = checker.projected_result(observed, turn, case, loads=decode)
            result = checker.verify_outcome(
                projected, case, manifest["config_sha256"], manifest["image"]
            )
            require(
                encode(result)
                == encode({k: v for k, v in row.items() if k not in {"session", "turn"}}),
                "gateway_projection_correlation",
            )
    require(encode(value["requests"]) == encode(requests), "gateway_request_correlation")
