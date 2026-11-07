"""Render a bounded Markdown request using only installed code and passive inline assets."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import runpy
import secrets
import signal
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
CSS = """
@page { margin: 19mm 16mm 21mm; }
* { box-sizing: border-box; }
body { font: 10pt/1.55 'DejaVu Sans', 'Noto Color Emoji', sans-serif; color: #17212b; }
header { border-bottom: 2px solid #284b63; margin-bottom: 24px; padding-bottom: 16px; }
header h1 { font-size: 25pt; line-height: 1.15; margin-bottom: 10px; }
header p { color: #526475; margin: 0; }
h1, h2, h3, h4, h5, h6 { break-after: avoid; line-height: 1.25; color: #213f53; }
p { orphans: 3; widows: 3; }
pre, code { font-family: 'DejaVu Sans Mono', monospace; font-size: 8.5pt; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f1f4f6; padding: 12px; }
code { overflow-wrap: anywhere; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 8.5pt; }
th, td { border: 1px solid #ccd5dc; padding: 6px; overflow-wrap: anywhere; }
th { background: #eaf0f4; text-align: left; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
img, svg { max-width: 100%; height: auto; }
figure { margin: 16px 0; break-inside: avoid; }
blockquote { border-left: 3px solid #b9cbd8; padding-left: 14px; margin-left: 0; }
a { color: #285d80; overflow-wrap: anywhere; }
.render-note { font-size: 9pt; color: #794c18; }
"""
RENDER_MERMAID = r"""async ({source, id}) => {
  const target = document.getElementById(id);
  try {
    if (/%%\s*\{|^\s*---/.test(source)) throw new Error('configuration is disabled');
    const result = await mermaid.render(id + '-svg', source);
    const parsed = new DOMParser().parseFromString(result.svg, 'image/svg+xml');
    if (parsed.querySelector('parsererror, script, foreignObject, image, a'))
      throw new Error('active diagram content');
    for (const element of parsed.querySelectorAll('*')) {
      for (const attr of element.attributes) {
        if (attr.name.toLowerCase().startsWith('on')) throw new Error('event attribute');
        if (/(?:^|:)href$/.test(attr.name) && !attr.value.startsWith('#'))
          throw new Error('external diagram reference');
      }
    }
    target.replaceChildren(document.importNode(parsed.documentElement, true));
    return true;
  } catch (_) {
    document.getElementById('d' + id + '-svg')?.remove();
    const note = document.createElement('p');
    note.className = 'render-note';
    note.textContent = 'Diagram could not be rendered; Mermaid source follows.';
    const code = document.createElement('code');
    code.textContent = source;
    const pre = document.createElement('pre');
    pre.appendChild(code);
    target.replaceChildren(note, pre);
    return false;
  }
}"""


def document_html(request, assets):
    """Disable raw HTML and resolve every Markdown image to an explicitly supplied SVG."""
    diagrams = []
    markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    fence = markdown.renderer.rules["fence"]

    def image(tokens, index, options, env):
        token = tokens[index]
        name = token.attrGet("src")
        if name not in assets:
            raise ValueError("Markdown images must name a supplied SVG asset")
        encoded = base64.b64encode(assets[name]).decode("ascii")
        return (
            f'<img alt="{html.escape(token.content, quote=True)}" '
            f'src="data:image/svg+xml;base64,{encoded}">'
        )

    def code(tokens, index, options, env):
        token = tokens[index]
        if token.info.strip() != "mermaid":
            return fence(tokens, index, options, env)
        identifier = f"diagram-{len(diagrams)}"
        diagrams.append({"id": identifier, "source": token.content})
        return f'<figure id="{identifier}"></figure>'

    markdown.renderer.rules["image"] = image
    markdown.renderer.rules["fence"] = code
    body = markdown.render(request["markdown"])
    nonce = secrets.token_hex(16)
    policy = (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
        f"script-src 'nonce-{nonce}'; connect-src 'none'; base-uri 'none'; form-action 'none'"
    )
    title, date = (html.escape(request[name]) for name in ("title", "date"))
    header = f"<header><h1>{title}</h1><p>{date}</p></header>" if title or date else ""
    policy = html.escape(policy, quote=True)
    return (
        (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
            f"<title>{title}</title><style>{CSS}</style></head><body>{header}{body}</body></html>"
        ),
        diagrams,
        nonce,
    )


def render(request, assets, *, executable="/usr/bin/chromium"):
    """Return the complete PDF and evidence, refusing bounds before exposing any output."""
    markup, diagrams, nonce = document_html(request, assets)
    requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable,
            args=["--disable-dev-shm-usage", "--disable-background-networking"],
        )
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)

            def intercept(route):
                if route.request.url.startswith("data:"):
                    route.continue_()
                else:
                    requests.append(route.request.url[:200])
                    route.abort()

            context.route("**/*", intercept)
            context.add_init_script("""(() => {
                window.blockedResources = [];
                document.addEventListener('securitypolicyviolation', event => {
                    window.blockedResources.push(event.blockedURI);
                });
            })()""")
            page = context.new_page()
            page.set_default_timeout(request["deadline_seconds"] * 1000)
            encoded_page = base64.b64encode(markup.encode("utf-8")).decode("ascii")
            page.goto("data:text/html;base64," + encoded_page, wait_until="load")
            page.evaluate(
                """({code, nonce}) => {
                const script = document.createElement('script');
                script.nonce = nonce; script.textContent = code; document.head.appendChild(script);
            }""",
                {"code": (ROOT / "mermaid.min.js").read_text("utf-8"), "nonce": nonce},
            )
            page.evaluate("""() => mermaid.initialize({
                startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
                maxTextSize: 50000, maxEdges: 500, htmlLabels: false,
                flowchart: {htmlLabels: false}, fontFamily: 'DejaVu Sans',
                theme: 'neutral'
            })""")
            rendered = sum(page.evaluate(RENDER_MERMAID, diagram) for diagram in diagrams)
            page.evaluate("document.fonts.ready")
            page.evaluate("""async () => {
                await Promise.all(Array.from(document.images, image => image.decode()));
            }""")
            data = page.pdf(
                format=request["page_size"],
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=(
                    '<div style="width:100%;text-align:center;font:8px sans-serif;color:#526475">'
                    'Page <span class="pageNumber"></span> of '
                    '<span class="totalPages"></span></div>'
                ),
                margin={"top": "19mm", "bottom": "21mm", "left": "16mm", "right": "16mm"},
            )
            requests.extend(page.evaluate("window.blockedResources"))
            if requests:
                raise ValueError("page attempted a forbidden resource request")
        finally:
            browser.close()
    if len(data) > request["max_pdf_bytes"]:
        raise ValueError("max_pdf_bytes exceeded")
    pages = len(PdfReader(io.BytesIO(data)).pages)
    if not 1 <= pages <= request["max_pages"]:
        raise ValueError("max_pages exceeded")
    return data, {
        "schema_version": 1,
        "pages": pages,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "network_requests": requests,
        "mermaid_rendered": rendered,
        "mermaid_fallbacks": len(diagrams) - rendered,
        "assets": len(assets),
    }


def main():
    """Read a staged request and publish outputs only after every guard passes."""
    for name in ("document.pdf", "export.json"):
        Path(name).unlink(missing_ok=True)
    try:
        if Path("request.json").stat().st_size > 26_000_000:
            raise ValueError("request too large")
        request = json.loads(Path("request.json").read_text("utf-8"))
        assets = runpy.run_path("_export_contract.py")["validate_request"](request)

        def expired(signum, frame):
            raise TimeoutError("render deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, request["deadline_seconds"])
        data, metadata = render(request, assets)
        Path("document.pdf").write_bytes(data)
        Path("export.json").write_text(json.dumps(metadata), "utf-8")
    except (ValueError, TimeoutError) as error:
        for name in ("document.pdf", "export.json"):
            Path(name).unlink(missing_ok=True)
        print(str(error)[:1024], file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
