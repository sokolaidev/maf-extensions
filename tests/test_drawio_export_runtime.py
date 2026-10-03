"""Offline resource policy is checked before starting the native renderer."""

import base64
import hashlib
import json
import runpy
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

import pytest

RUNTIME = runpy.run_path(str(Path(__file__).parents[1] / "images/drawio-export/export.py"))


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"],
)
@pytest.mark.parametrize("embedded", [False, True])
def test_encoded_xml_declarations_are_refused_before_parsing(monkeypatch, encoding, embedded):
    xml = '<!DOCTYPE svg [<!ENTITY text "expanded">]><svg xmlns="http://www.w3.org/2000/svg"><text>&text;</text></svg>'

    def parse(*args, **kwargs):
        pytest.fail("DTD/entity content must not reach the XML parser")

    monkeypatch.setattr(RUNTIME["ET"], "fromstring", parse)
    with pytest.raises(ValueError):
        if embedded:
            uri = "data:image/svg+xml;base64," + base64.b64encode(xml.encode(encoding)).decode()
            RUNTIME["image_data"](uri)
        else:
            RUNTIME["xml_document"](xml.encode(encoding))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_utf8_xml_preserves_unicode(encoding):
    xml = '<svg xmlns="http://www.w3.org/2000/svg"><text>Résumé Ω</text></svg>'
    root = RUNTIME["xml_document"](xml.encode(encoding))
    assert root[0].text == "Résumé Ω"


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://x";</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><use href="#x"/></svg>',
        '<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>',
    ],
)
def test_embedded_svg_refuses_active_or_recursive_resources(svg):
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    with pytest.raises(ValueError):
        RUNTIME["image_data"](uri)


@pytest.mark.parametrize(
    "attribute",
    [
        'style="image=https://example.invalid/image.svg;"',
        'style="image=file:///etc/passwd;"',
        'style="fontFamily=Missing;"',
        'style="fillColor=url(https://example.invalid);"',
        'value="&lt;img src=&quot;https://example.invalid&quot;&gt;"',
        'value="&lt;span style=&quot;background-image:url(x)&quot;&gt;x&lt;/span&gt;"',
        'link="https://example.invalid"',
        'math="1"',
    ],
)
def test_document_refuses_unresolved_resources(attribute):
    with pytest.raises(ValueError):
        RUNTIME["prepare_document"](
            f"<mxfile><mxCell {attribute}/></mxfile>",
            {"assets": {}, "fonts": {"DejaVu Sans": "unused"}},
        )


def test_safe_embedded_svg_and_formatting_survive():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1"/></svg>'
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    assert RUNTIME["image_data"](uri) == uri
    result = RUNTIME["prepare_document"](
        '<mxfile><mxCell value="&lt;b&gt;Résumé Ω&lt;/b&gt;" style="fontFamily=Arial;"/></mxfile>',
        {"assets": {}, "fonts": {"DejaVu Sans": "unused"}},
    )
    assert "DejaVu Sans" in result and "Résumé Ω" in result


@pytest.mark.parametrize("size", [(0, 1), (4097, 1), (4001, 4000)])
def test_dimensions_refuse_excessive_allocations(size):
    with pytest.raises(ValueError):
        RUNTIME["dimensions"](*size)


def test_drawio_embedded_image_style_does_not_split_at_base64_marker():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1"/></svg>'
    payload = base64.b64encode(svg.encode()).decode()
    for marker in ("", ";base64"):
        prepared = RUNTIME["prepare_document"](
            f'<mxfile><mxCell style="shape=image;image=data:image/svg+xml{marker},{payload};"/></mxfile>',
            {"assets": {}, "fonts": {"DejaVu Sans": "unused"}},
        )
        assert f"image=data:image/svg+xml,{payload};" in prepared
        assert ";base64," not in prepared


def test_native_svg_declarations_and_warning_become_offline_content():
    data = (
        RUNTIME["SVG_DOCTYPE"]
        + b"""<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"><g
        requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility"/>
        <a xlink:href="https://www.drawio.com/doc/faq/svg-export-text-problems">
        <text>Text is not SVG - cannot display</text></a></svg>"""
    )
    root = RUNTIME["check_svg"](data, exported=True)
    assert not list(root.iter("{http://www.w3.org/2000/svg}a"))


@pytest.mark.parametrize("key", ["image", "indicatorImage"])
@pytest.mark.parametrize(
    "resource",
    [
        "/etc/passwd",
        "../../secret.png",
        "relative.png",
        "file:///etc/passwd",
        "https://example.invalid/icon.png",
    ],
)
def test_image_styles_refuse_unlisted_resources(key, resource):
    with pytest.raises(ValueError):
        RUNTIME["prepare_document"](
            f'<mxfile><mxCell style="{key}={resource};"/></mxfile>',
            {"assets": {}, "fonts": {"DejaVu Sans": "unused"}},
        )


@pytest.mark.parametrize("key", ["image", "indicatorImage"])
@pytest.mark.parametrize(
    "reference", ["img/lib/icon.svg", "https://app.diagrams.net/img/lib/icon.svg", "data"]
)
def test_image_styles_embed_verified_assets(tmp_path, monkeypatch, key, reference):
    data = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1"/></svg>'
    asset = tmp_path / "assets/img/lib/icon.svg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(data)
    monkeypatch.setitem(RUNTIME["prepare_document"].__globals__, "ROOT", tmp_path)
    encoded = base64.b64encode(data).decode()
    if reference == "data":
        reference = "data:image/svg+xml;base64," + encoded
    prepared = RUNTIME["prepare_document"](
        f'<mxfile><mxCell style="{key}={reference};"/></mxfile>',
        {
            "assets": {"img/lib/icon.svg": hashlib.sha256(data).hexdigest()},
            "fonts": {"DejaVu Sans": "unused"},
        },
    )
    assert f"{key}=data:image/svg+xml,{encoded};" in prepared


def test_oversized_png_header_is_a_resource_refusal():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 6000, 6000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0"))
        + chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="Invalid embedded image"):
        RUNTIME["image_data"]("data:image/png;base64," + base64.b64encode(data).decode())


@pytest.fixture
def font_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(RUNTIME["export_document"].__globals__, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    original_read = Path.read_bytes

    def read(path):
        return (
            b"asar" if path.as_posix() == "/opt/drawio/resources/app.asar" else original_read(path)
        )

    monkeypatch.setattr(Path, "read_bytes", read)
    variants = []
    for index in range(4):
        path = tmp_path / f"font-{index}.ttf"
        path.write_bytes(b"font")
        variants.append(
            {
                "path": str(path),
                "weight": 400 if index < 2 else 700,
                "style": "normal" if index % 2 == 0 else "italic",
                "sha256": hashlib.sha256(b"font").hexdigest(),
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "desktop": "31.7.0",
                "asar_sha256": hashlib.sha256(b"asar").hexdigest(),
                "assets": {},
                "fonts": {"DejaVu Sans": variants[0]["path"]},
                "font_variants": {"DejaVu Sans": variants},
            }
        )
    )
    return variants


@pytest.mark.parametrize("format", ["png", "jpg", "svg"])
@pytest.mark.parametrize("variant", range(4))
@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_all_formats_preflight_every_font_variant(
    tmp_path, monkeypatch, font_runtime, format, variant, damage
):
    damaged = Path(font_runtime[variant]["path"])
    if damage == "missing":
        damaged.unlink()
    else:
        damaged.write_bytes(b"changed")

    def render(*args):
        pytest.fail("Renderer must not start with a missing or modified font")

    monkeypatch.setitem(RUNTIME["export_document"].__globals__, "run_renderer", render)
    with pytest.raises((FileNotFoundError, RuntimeError)):
        RUNTIME["export_document"](
            '<mxfile><diagram><mxCell style="fontFamily=Arial;"/></diagram></mxfile>',
            {
                "formats": [format],
                "pages": None,
                "scale": 1,
                "transparent": False,
                "jpeg_quality": 90,
            },
            time.monotonic() + 10,
        )
    assert not (tmp_path / "exports.json").exists()


@pytest.mark.parametrize(
    "attribute",
    [
        'value="fontFamily=Missing;"',
        'label="fontFamily=Missing;"',
        'custom="fontFamily=Missing;"',
        'style="custom=fontFamily=Missing;"',
        'value="fontFamily=Missing"',
    ],
)
@pytest.mark.parametrize("format", ["png", "jpg", "svg"])
def test_font_mentions_outside_font_style_do_not_change_preflight(
    monkeypatch, font_runtime, attribute, format
):
    class RendererReached(Exception):
        pass

    def render(*args):
        raise RendererReached

    monkeypatch.setitem(RUNTIME["export_document"].__globals__, "run_renderer", render)
    with pytest.raises(RendererReached):
        RUNTIME["export_document"](
            f"<mxfile><diagram><mxCell {attribute}/></diagram></mxfile>",
            {
                "formats": [format],
                "pages": None,
                "scale": 1,
                "transparent": False,
                "jpeg_quality": 90,
            },
            time.monotonic() + 10,
        )


@pytest.mark.skipif(
    shutil.which("node") is None, reason="requires Node.js for the native export guard"
)
@pytest.mark.parametrize("key", ["shape", "resIcon", "indicatorShape"])
@pytest.mark.parametrize("shape", ["missing", "rectangle", "registered"])
def test_native_shape_guard_checks_each_registry(key, shape):
    node = shutil.which("node")
    assert node is not None
    script = """
const fs = require('fs');
var mxConstants = {STYLE_SHAPE: 'shape', STYLE_INDICATOR_SHAPE: 'indicatorShape'};
var mxStencilRegistry = {getStencil: name => name === 'registered'};
function mxGraph() {}
mxGraph.prototype.getIndicatorImage = state => state.style.indicatorImage;
function mxCellRenderer() {}
mxCellRenderer.prototype.createShape = () => ({});
mxCellRenderer.defaultShapes = {rectangle: true};
var sent = [];
var electron = {sendMessage: (channel, value) => sent.push([channel, value])};
eval(fs.readFileSync(process.argv[1], 'utf8'));
for (const media of ['png', 'jpeg', 'svg+xml']) {
    const uri = 'data:image/' + media + ',AAAA';
    const restored = new mxGraph().getIndicatorImage({style: {indicatorImage: uri}});
    if (restored !== uri.replace(',', ';base64,')) throw Error('Invalid indicator data URI');
}
new mxCellRenderer().createShape({style: {[process.argv[2]]: process.argv[3]}});
mafSend('render-finished', {bounds: JSON.stringify({x: 0, y: 0, width: 10, height: 10})});
console.log(JSON.stringify(sent));
"""
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(Path(__file__).parents[1] / "images/drawio-export/guard.js"),
            key,
            shape,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    [(channel, value)] = json.loads(result.stdout)
    refused = shape == "missing" or (key == "indicatorShape" and shape == "registered")
    assert channel == ("export-error" if refused else "render-finished")
    if refused:
        assert value.startswith("MAF_REFUSED:")
