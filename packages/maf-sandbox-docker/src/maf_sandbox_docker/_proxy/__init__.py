"""The packaged build context for the egress proxy image.

The proxy is shipped as source, not as an image: the package carries the Dockerfile and the
script, the developer builds the image on their own machine, and the pin they trust is the
base image named in the Dockerfile — the same supply pattern as the sample's Bicep image.

The Dockerfile and script are pinned byte-identical to ``maf-sandbox-wslc``'s copy by a test
in each package (maintainer ruling: each backend ships its own copy rather than a hoisted
shared one, so that a fix landing in one without the other fails loudly instead of drifting).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["build_context"]


def build_context() -> Path:
    """The directory ``docker build`` needs — it holds the Dockerfile and the proxy script."""
    return Path(__file__).parent
