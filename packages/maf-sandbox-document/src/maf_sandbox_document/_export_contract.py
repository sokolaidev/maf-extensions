"""Bounded, passive SVG and request validation shared with the export guest."""

from __future__ import annotations

import base64
import math
import re
import xml.etree.ElementTree as ET
from typing import Any, cast

MAXIMA = {
    "max_markdown_bytes": 2_000_000,
    "max_asset_bytes": 10_000_000,
    "max_assets": 100,
    "max_pages": 500,
    "max_pdf_bytes": 32_000_000,
}
SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
MAX_SVG_DEPTH = 128
MAX_SVG_ELEMENTS = 100_000
ELEMENTS = frozenset(
    f"{{{SVG}}}{name}"
    for name in (
        "svg g defs path rect circle ellipse line polyline polygon text tspan title desc "
        "linearGradient radialGradient stop clipPath mask marker use symbol"
    ).split()
)
ATTRIBUTES = frozenset(
    "id viewBox width height x y x1 x2 y1 y2 cx cy r rx ry d points transform "
    "fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap "
    "stroke-linejoin stroke-dasharray stroke-dashoffset opacity clip-path clip-rule "
    "mask marker-start marker-mid marker-end markerWidth markerHeight refX refY orient "
    "markerUnits preserveAspectRatio gradientUnits gradientTransform offset stop-color "
    "stop-opacity spreadMethod font-family font-size font-weight font-style text-anchor "
    "dominant-baseline dx dy href".split()
)


class _PassiveSVG:
    """Validate parser events without retaining an element tree."""

    def __init__(self) -> None:
        self.depth = 0
        self.elements = 0

    def start_ns(self, prefix: str, uri: str) -> None:
        if uri not in {SVG, XLINK}:
            raise ValueError("unsupported SVG namespace")

    def start(self, tag: str, attributes: dict[str, str]) -> None:
        if self.depth == 0 and tag != f"{{{SVG}}}svg":
            raise ValueError("asset is not SVG")
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_SVG_DEPTH or self.elements > MAX_SVG_ELEMENTS:
            raise ValueError("SVG structural limits exceeded")
        if tag not in ELEMENTS:
            raise ValueError("unsupported or active SVG element")
        for key, value in attributes.items():
            if key == f"{{{XLINK}}}href":
                key = "href"
            if key not in ATTRIBUTES:
                raise ValueError("unsupported or active SVG attribute")
            if key == "href" and not re.fullmatch(r"#[A-Za-z_][\w.-]*", value):
                raise ValueError("SVG references must be local fragments")
            if re.search(r"url\s*\(", value, re.I) and not re.fullmatch(
                r"url\(#[A-Za-z_][\w.-]*\)", value, re.I
            ):
                raise ValueError("SVG resources must be local fragments")
            if any(character in value for character in "\\\x00"):
                raise ValueError("unsupported SVG attribute value")

    def end(self, tag: str) -> None:
        self.depth -= 1


def validate_svg(data: bytes) -> None:
    """Apply export_pdf's passive-SVG check, returning None or raising ValueError.

    Input must be UTF-8 SVG bytes. This does not check asset names or ExportOptions byte/count caps.
    """
    # NULs allow UTF-16 autodetection to bypass the UTF-8 byte guards.
    if b"\x00" in data:
        raise ValueError("asset is not UTF-8 SVG")
    if not data or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)|<\?(?!xml\s)", data, re.I):
        raise ValueError("assets must be passive SVG without DTDs or entities")
    try:
        parser = ET.XMLParser(encoding="utf-8", target=_PassiveSVG())  # nosec B314
        parser.feed(data.decode("utf-8"))
        parser.close()
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError("asset is not UTF-8 SVG") from exc


def is_valid_asset_name(name: str) -> bool:
    """Return whether a name meets export_pdf's relative SVG naming rule and 256-character cap."""
    return (
        isinstance(cast(object, name), str)
        and len(name) <= 256
        and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.svg", name) is not None
    )


def validate_request(request: dict[str, Any]) -> dict[str, bytes]:
    """Validate the wire request and return decoded assets within the host's caps."""
    for name, ceiling in MAXIMA.items():
        value = request.get(name)
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer in [1, {ceiling}]")
    deadline: Any = request.get("deadline_seconds")
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or not 0 < deadline <= 300:
        raise ValueError("deadline_seconds must be finite and in (0, 300]")
    if request.get("page_size") not in ("A4", "Letter"):
        raise ValueError("page_size must be A4 or Letter")
    if request.get("unsupplied_images", "refuse") not in ("refuse", "placeholder"):
        raise ValueError("unsupplied_images must be refuse or placeholder")
    for name in ("title", "date"):
        if not isinstance(request.get(name), str) or len(request[name].encode("utf-8")) > 1024:
            raise ValueError(f"{name} must be text of at most 1024 UTF-8 bytes")
    markdown = request.get("markdown")
    if not isinstance(markdown, str):
        raise ValueError("markdown must be text")
    if len(markdown.encode("utf-8")) > request["max_markdown_bytes"]:
        raise ValueError("max_markdown_bytes exceeded")
    assets = request.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("assets is not a mapping")
    assets = cast(dict[str, Any], assets)
    if len(assets) > request["max_assets"]:
        raise ValueError("max_assets exceeded or assets is not a mapping")
    decoded: dict[str, bytes] = {}
    total = 0
    for name, encoded in assets.items():
        if not is_valid_asset_name(name):
            raise ValueError("asset names must be relative SVG names without traversal")
        if not isinstance(encoded, str) or len(encoded) > (request["max_asset_bytes"] + 2) // 3 * 4:
            raise ValueError("max_asset_bytes exceeded")
        data = base64.b64decode(encoded, validate=True)
        total += len(data)
        if total > request["max_asset_bytes"]:
            raise ValueError("max_asset_bytes exceeded")
        validate_svg(data)
        decoded[name] = data
    return decoded
