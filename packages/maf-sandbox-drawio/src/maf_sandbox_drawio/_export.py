"""Host-selected limits for the optional offline renderer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

ExportFormat = Literal["png", "jpg", "svg"]
EXPORT_FILE_BYTES = 8 * 1024 * 1024
EXPORT_TOTAL_BYTES = 32 * 1024 * 1024
EXPORT_FILES = 25
EXPORT_MANIFEST_BYTES = 4096


@dataclass(frozen=True)
class DrawioExport:
    """Export selected pages using the installed offline runtime; page numbers start at one.

    None selects every page. The host supplies a prepared POSIX runtime; image=None is valid
    only when the chosen backend already provides that runtime and the required file channels.
    """

    formats: tuple[ExportFormat, ...] = ("png",)
    pages: tuple[int, ...] | None = None
    scale: float = 1.0
    transparent: bool = False
    jpeg_quality: int = 90

    def __post_init__(self) -> None:
        if (
            not isinstance(cast(object, self.formats), tuple)
            or not self.formats
            or any(value not in ("png", "jpg", "svg") for value in self.formats)
            or len(set(self.formats)) != len(self.formats)
        ):
            raise ValueError("formats must be a nonempty tuple of distinct png, jpg or svg values")
        if self.pages is not None and (
            not isinstance(cast(object, self.pages), tuple)
            or not self.pages
            or any(type(page) is not int or not 1 <= page <= 8 for page in self.pages)
            or len(set(self.pages)) != len(self.pages)
        ):
            raise ValueError(
                "pages must be None or a nonempty tuple of distinct integers in [1, 8]"
            )
        if (
            type(self.scale) not in (int, float)
            or not math.isfinite(self.scale)
            or not 0 < self.scale <= 4
        ):
            raise ValueError("scale must be finite and in (0, 4]")
        if type(self.transparent) is not bool:
            raise TypeError("transparent must be a bool")
        if type(self.jpeg_quality) is not int or not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be an integer in [1, 100]")
