"""Verify that the draw.io PNG live check requires artifacts from a clean call."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_live_drawio_png_sample import verify  # noqa: E402


@pytest.fixture
def export(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    drawio = tmp_path / "diagram.drawio"
    drawio.write_text("<mxfile/>", encoding="utf-8")
    png = tmp_path / "diagram-1.png"
    with Image.new("RGB", (40, 20)) as image:
        image.save(png)
    return tmp_path, {
        "tool": "create_drawio",
        "seconds": 0.5,
        "unclean": 0,
        "undisposed": False,
        "width": 40,
        "height": 20,
        "drawio_sha256": hashlib.sha256(drawio.read_bytes()).hexdigest(),
        "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
    }


def test_accepts_a_clean_export(export: tuple[Path, dict[str, object]]) -> None:
    output, evidence = export
    verify(f"  [measured] {json.dumps(evidence)}", output)


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "export_drawio"},
        {"unclean": 1},
        {"undisposed": True},
        {"seconds": -1},
        {"width": 41},
        {"drawio_sha256": "wrong"},
        {"png_sha256": "wrong"},
    ],
)
def test_rejects_mismatched_evidence(
    export: tuple[Path, dict[str, object]], change: dict[str, object]
) -> None:
    output, evidence = export
    with pytest.raises(ValueError):
        verify(f"  [measured] {json.dumps(evidence | change)}", output)


def test_rejects_replaced_png(export: tuple[Path, dict[str, object]]) -> None:
    output, evidence = export
    with Image.new("RGB", (40, 20), color="blue") as image:
        image.save(output / "diagram-1.png")
    with pytest.raises(ValueError, match="differ from the measured export"):
        verify(f"  [measured] {json.dumps(evidence)}", output)
