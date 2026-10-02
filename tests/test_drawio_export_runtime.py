"""Offline resource policy is checked before starting the native renderer."""

import base64
import runpy
from pathlib import Path

import pytest

RUNTIME = runpy.run_path(str(Path(__file__).parents[1] / "images/drawio-export/export.py"))


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
