"""Offline renderer guards and opt-in native Chromium resource-denial checks."""

import os
import runpy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

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


def test_repeated_assets_refused_before_encoding_excess_copies(monkeypatch):
    namespace = RENDERER["document_html"].__globals__
    monkeypatch.setitem(namespace, "MAX_MARKUP_BYTES", 1000)
    encoded = MagicMock(wraps=namespace["base64"].b64encode)
    monkeypatch.setattr(namespace["base64"], "b64encode", encoded)
    with pytest.raises(ValueError, match="expanded markup"):
        markup("![Asset](diagram.svg)\n" * 100, {"diagram.svg": SVG * 3})
    assert encoded.call_count < 4


def test_non_image_markup_is_bounded(monkeypatch):
    monkeypatch.setitem(RENDERER["document_html"].__globals__, "MAX_MARKUP_BYTES", 100)
    with pytest.raises(ValueError, match="expanded markup"):
        markup("&" * 100)


@pytest.mark.parametrize("url", ["https://example.invalid/asset", "file:///private/asset"])
def test_renderer_aborts_forbidden_requests_and_refuses_pdf(monkeypatch, tmp_path, url):
    namespace = RENDERER["render"].__globals__
    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    route = MagicMock()
    route.request.url = url
    page.goto.side_effect = lambda *args, **kwargs: context.route.call_args.args[1](route)
    page.evaluate.return_value = []
    page.pdf.return_value = b"%PDF-fixture"
    (tmp_path / "mermaid.min.js").write_text("", encoding="utf-8")
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "sync_playwright", lambda: playwright)
    with pytest.raises(ValueError, match="forbidden resource"):
        RENDERER["render"](asdict(ExportOptions()) | {"markdown": "report"}, {})
    route.abort.assert_called_once()
    route.continue_.assert_not_called()
    browser.close.assert_called_once()


@pytest.mark.parametrize("csp", [False, True], ids=["interception", "csp"])
def test_native_browser_resource_denial(monkeypatch, tmp_path, csp):
    executable = os.environ.get("MAF_DOCUMENT_EXPORT_BROWSER")
    if not executable:
        pytest.skip("requires MAF_DOCUMENT_EXPORT_BROWSER opt-in")
    namespace = RENDERER["render"].__globals__
    policy = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\';">'
    document = (policy if csp else "") + '<iframe src="https://example.invalid/asset"></iframe>'
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "document_html", lambda *args: (document, [], "nonce"))
    # Initialization stays trusted while the controlled document exercises the browser boundary.
    (tmp_path / "mermaid.min.js").write_text(
        "globalThis.mermaid = {initialize() {}};", encoding="utf-8"
    )
    if csp:
        document = document.replace(
            "default-src 'none';", "default-src 'none'; script-src 'nonce-nonce';"
        )
    with pytest.raises(ValueError, match="forbidden resource"):
        RENDERER["render"](
            asdict(ExportOptions()) | {"markdown": "report"}, {}, executable=executable
        )
