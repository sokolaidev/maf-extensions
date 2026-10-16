"""Independent CLI observations for the live sbx assertions."""

from __future__ import annotations

import json
from typing import cast


def listed_rows(stdout: bytes, key: str) -> list[dict[str, object]]:
    """Read either listing format without treating an unknown response as an empty list."""
    payload = json.loads(stdout)
    rows = payload.get(key) if isinstance(payload, dict) else payload
    assert isinstance(rows, list), f"expected a direct array or a {key!r} list: {payload!r}"
    assert all(isinstance(row, dict) for row in rows), f"expected object rows: {rows!r}"
    return cast("list[dict[str, object]]", rows)
