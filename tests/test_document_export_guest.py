"""Print markup safety and deterministic rendering inputs, without a browser or network."""

import runpy
from dataclasses import asdict
from pathlib import Path

import pytest
from maf_sandbox_document import ExportOptions

RENDERER = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "images/document-export/render.py")
)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Asset</text></svg>'


def markup(markdown, assets=None):
    return RENDERER["document_html"](
        asdict(ExportOptions(title="A <report>", date="2026-11-07")) | {"markdown": markdown},
        assets or {},
    )


def test_raw_html_escaped_and_gfm_tables_enabled():
    page, _, _ = markup("<script>alert(1)</script>\n\n| A | B |\n|---|---|\n| one | two |")
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "<table>" in page
    assert "A &lt;report&gt;" in page


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/a.svg",
        "//example.invalid/a.svg",
        "missing.svg",
        "../a.svg",
    ],
)
def test_images_cannot_load_outside_supplied_assets(reference):
    with pytest.raises(ValueError, match="supplied SVG"):
        markup(f"![image]({reference})")


def test_supplied_svg_is_embedded_without_a_url_fetch():
    page, _, _ = markup("![Asset](diagram.svg)", {"diagram.svg": SVG})
    assert 'src="data:image/svg+xml;base64,' in page
    assert 'src="diagram.svg"' not in page


def test_mermaid_is_data_and_not_interpolated_as_html_or_javascript():
    source = "</script><script>alert(1)</script>"
    page, diagrams, nonce = markup(f"```mermaid\n{source}\n```\n\n```python\nprint(1)\n```")
    assert source not in page
    assert diagrams == [{"id": "diagram-0", "source": source + "\n"}]
    assert "print(1)" in page
    assert nonce in page


@pytest.mark.parametrize("reference", ["file:///etc/passwd", "data:image/svg+xml;base64,PHN2Zz4="])
def test_disallowed_markdown_schemes_stay_literal(reference):
    page, _, _ = markup(f"![image]({reference})")
    assert "<img" not in page
