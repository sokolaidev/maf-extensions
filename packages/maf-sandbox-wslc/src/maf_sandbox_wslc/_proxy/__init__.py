"""The packaged build context for the egress proxy image.

The package carries a pinned iron-proxy build and its local policy patch. The host builds the
image before enabling ALLOWLIST.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["build_context"]


def build_context() -> Path:
    """The directory ``wslc build`` needs for the pinned proxy image."""
    return Path(__file__).parent
