"""Offline renderer guards and opt-in native Chromium resource-denial checks."""

import io
import os
import runpy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from maf_sandbox_document import ExportOptions
from pypdf import PdfReader

RENDERER = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "images/document-export/render.py")
)
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="50">'
    b'<text x="10" y="30">Asset</text></svg>'
)


def markup(markdown, assets=None, **options):
    return RENDERER["document_html"](
        asdict(ExportOptions(title="A <report>", date="2026-11-07", **options))
        | {"markdown": markdown},
        assets or {},
    )


def test_raw_html_escaped_and_gfm_tables_enabled():
    page, _, _, _ = markup("<script>alert(1)</script>\n\n| A | B |\n|---|---|\n| one | two |")
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
    page, _, _, _ = markup("![Asset](diagram.svg)", {"diagram.svg": SVG})
    assert 'src="data:image/svg+xml;base64,' in page
    assert 'src="diagram.svg"' not in page


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/a.svg",
        "//example.invalid/a.svg",
        "missing.svg",
        "../a.svg",
        "a.png",
    ],
)
def test_unsupplied_images_become_text_only_in_placeholder_mode(reference):
    page, _, _, count = markup(f"![missing]({reference})", unsupplied_images="placeholder")
    assert "<p>[Image: missing]</p>" in page
    assert "<img" not in page
    assert reference not in page
    assert count == 1


@pytest.mark.parametrize(
    "source,expected,count",
    [
        ("![a `]` b](missing.svg)", "[Image: a ] b]", 1),
        (
            '![one](one.svg "a ` title") ![two](two.svg "another ` title")',
            "[Image: one] [Image: two]",
            2,
        ),
        (
            "| A | B |\n|---|---|\n| ` ![one](one.svg) | ![two](two.svg) ` |",
            "<td>` [Image: one]</td>\n<td>[Image: two] `</td>",
            2,
        ),
        ('![one][ref] ![two][ref]\n\n[ref]: missing.svg "title"', "[Image: one] [Image: two]", 2),
        (
            "![*bold* **text** &amp; `<tag>`](missing.svg)",
            "[Image: bold text &amp; &lt;tag&gt;]",
            1,
        ),
        ("![](missing.svg)", "[Image: ]", 1),
        ("![outer ![inner](inner.svg)](outer.svg)", "[Image: outer inner]", 1),
        ("`![code](missing.svg)`", "<code>![code](missing.svg)</code>", 0),
        ("```\n![code](missing.svg)\n```", "![code](missing.svg)", 0),
        (r"\![escaped](missing.svg)", '!<a href="missing.svg">escaped</a>', 0),
        (r"\\![image](missing.svg)", "[Image: image]", 1),
        ("![undefined][ref]", "![undefined][ref]", 0),
    ],
)
def test_placeholder_follows_parser_tokens(source, expected, count):
    page, _, _, replaced = markup(source, unsupplied_images="placeholder")
    assert expected in page
    assert replaced == count
    assert "<img" not in page


def test_placeholder_count_excludes_supplied_images_and_resets_per_document():
    source = "![supplied](diagram.svg) ![missing](other.svg) ![missing](other.svg)"
    page, _, _, count = markup(source, {"diagram.svg": SVG}, unsupplied_images="placeholder")
    assert page.count('src="data:image/svg+xml;base64,') == 1
    assert page.count("[Image: missing]") == count == 2
    assert (
        markup("![supplied](diagram.svg)", {"diagram.svg": SVG}, unsupplied_images="placeholder")[3]
        == 0
    )


def test_placeholder_alt_cannot_inject_html():
    page, _, _, count = markup(
        '![<img src="https://example.invalid" onerror="alert(1)">](x.svg)',
        unsupplied_images="placeholder",
    )
    assert "[Image: &lt;img src=&quot;https://example.invalid&quot;" in page
    assert "<img" not in page
    assert count == 1


def test_placeholder_expansion_remains_bounded(monkeypatch):
    monkeypatch.setitem(RENDERER["document_html"].__globals__, "MAX_MARKUP_BYTES", 100)
    with pytest.raises(ValueError, match="expanded markup"):
        markup("![é &amp;](missing.svg) " * 20, unsupplied_images="placeholder")


def test_mermaid_is_data_and_not_interpolated_as_html_or_javascript():
    source = "</script><script>alert(1)</script>"
    page, diagrams, nonce, _ = markup(f"```mermaid\n{source}\n```\n\n```python\nprint(1)\n```")
    assert source not in page
    assert diagrams == [{"id": "diagram-0", "source": source + "\n"}]
    assert "print(1)" in page
    assert nonce in page


@pytest.mark.parametrize("reference", ["file:///etc/passwd", "data:image/svg+xml;base64,PHN2Zz4="])
@pytest.mark.parametrize("mode", ["refuse", "placeholder"])
def test_disallowed_markdown_schemes_stay_literal(reference, mode):
    page, _, _, count = markup(f"![image]({reference})", unsupplied_images=mode)
    assert "<img" not in page
    assert count == 0


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


@pytest.mark.parametrize("attribute", ["href", "title"])
@pytest.mark.parametrize("separator", [" ", "\n\n"], ids=["inline", "blocks"])
def test_reference_links_refused_before_accumulating_copies(monkeypatch, attribute, separator):
    namespace = RENDERER["document_html"].__globals__
    monkeypatch.setitem(namespace, "MAX_MARKUP_BYTES", 1000)
    renderer = namespace["RendererHTML"]
    original = renderer.renderToken
    rendered_links = []

    def observe(self, tokens, index, options, env):
        if tokens[index].type == "link_open":
            rendered_links.append(tokens[index].attrGet(attribute))
        return original(self, tokens, index, options, env)

    monkeypatch.setattr(renderer, "renderToken", observe)
    destination = "https://example.invalid/" + ("x" * 300 if attribute == "href" else "")
    title = ' "' + "&" * 100 + '"' if attribute == "title" else ""
    source = separator.join(["[link][ref]"] * 100) + f"\n\n[ref]: {destination}{title}"
    with pytest.raises(ValueError, match="expanded markup"):
        markup(source)
    assert 1 < len(rendered_links) < 5


def test_markup_budget_counts_unicode_bytes_across_tokens(monkeypatch):
    namespace = RENDERER["document_html"].__globals__
    source = "**é** *é*"
    body = "<p><strong>é</strong> <em>é</em></p>\n"
    monkeypatch.setitem(namespace, "MAX_MARKUP_BYTES", len(body.encode("utf-8")))
    assert body in markup(source)[0]
    monkeypatch.setitem(namespace, "MAX_MARKUP_BYTES", len(body.encode("utf-8")) - 1)
    with pytest.raises(ValueError, match="expanded markup"):
        markup(source)


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


@pytest.mark.parametrize("mode,count", [("refuse", 0), ("placeholder", 2)])
def test_renderer_reports_replacement_count(monkeypatch, tmp_path, mode, count):
    namespace = RENDERER["render"].__globals__
    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.launch.return_value
    page = browser.new_context.return_value.new_page.return_value
    page.evaluate.return_value = []
    page.pdf.return_value = b"%PDF-fixture"
    (tmp_path / "mermaid.min.js").write_text("", encoding="utf-8")
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "sync_playwright", lambda: playwright)
    monkeypatch.setitem(namespace, "PdfReader", lambda _: MagicMock(pages=[None]))
    source = "![missing](missing.svg) " * 2 if count else "![supplied](diagram.svg)"
    _, metadata = RENDERER["render"](
        asdict(ExportOptions(unsupplied_images=mode)) | {"markdown": source}, {"diagram.svg": SVG}
    )
    assert metadata["images_replaced"] == count
    assert metadata["network_requests"] == []
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
    monkeypatch.setitem(namespace, "document_html", lambda *args: (document, [], "nonce", 0))
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


def test_native_browser_placeholder_pdf_and_metadata(monkeypatch, tmp_path):
    executable = os.environ.get("MAF_DOCUMENT_EXPORT_BROWSER")
    if not executable:
        pytest.skip("requires MAF_DOCUMENT_EXPORT_BROWSER opt-in")
    monkeypatch.setitem(RENDERER["render"].__globals__, "ROOT", tmp_path)
    (tmp_path / "mermaid.min.js").write_text(
        "globalThis.mermaid = {initialize() {}};", encoding="utf-8"
    )
    request = asdict(ExportOptions(unsupplied_images="placeholder")) | {
        "markdown": "![Remote](https://example.invalid/a.png)\n\n![Local](missing.svg)\n\n"
        "![Supplied](diagram.svg)"
    }
    data, metadata = RENDERER["render"](request, {"diagram.svg": SVG}, executable=executable)
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    assert "[Image: Remote]" in text
    assert "[Image: Local]" in text
    assert "[Image: Supplied]" not in text
    assert "Asset" in text
    assert metadata["images_replaced"] == 2
    assert metadata["network_requests"] == []
