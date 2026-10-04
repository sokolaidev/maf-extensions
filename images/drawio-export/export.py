"""Offline Draw.io export inside a prepared POSIX sandbox."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

ROOT = Path("/opt/maf-drawio")
RENDERER_ID = 10001
MAX_FILE = 8 * 1024 * 1024
MAX_PREPARED = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
SVG_DOCTYPE = (
    b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
    b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
)
FONT_ALIASES = {
    "Helvetica": "DejaVu Sans",
    "Arial": "DejaVu Sans",
    "sans-serif": "DejaVu Sans",
    "Times New Roman": "DejaVu Serif",
    "serif": "DejaVu Serif",
    "Courier New": "DejaVu Sans Mono",
    "monospace": "DejaVu Sans Mono",
}
UNSAFE = re.compile(
    r"(?:https?:|file:|ftp:|javascript:|data:|@import|(?:url|src|image-set|expression)\s*\(|\\)",
    re.I,
)
SVG_FONT = re.compile(r"\bfont\b|local\s*\(", re.I)
SVG_ANIMATION = re.compile(r"\b(?:animation|transition|keyframes)\b", re.I)


class Label(HTMLParser):
    """Accept formatting-only HTML; resource-bearing label elements are refused."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {
            "b",
            "strong",
            "i",
            "em",
            "u",
            "s",
            "strike",
            "br",
            "p",
            "div",
            "span",
            "sub",
            "sup",
            "font",
        }:
            raise ValueError("Unsupported HTML label element")
        for name, value in attrs:
            if name not in {"style", "color", "size"} or UNSAFE.search(value or ""):
                raise ValueError("Unsupported HTML label attribute")
            if name == "style":
                for declaration in (value or "").split(";"):
                    if not declaration.strip():
                        continue
                    key, sep, setting = declaration.partition(":")
                    if (
                        not sep
                        or key.strip().lower()
                        not in {
                            "color",
                            "background-color",
                            "font-size",
                            "font-weight",
                            "font-style",
                            "text-decoration",
                            "text-align",
                            "white-space",
                            "line-height",
                        }
                        or not re.fullmatch(r"[\w\s#.,()%+\-]+", setting)
                    ):
                        raise ValueError("Unsupported HTML label style")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_decl(self, decl: str) -> None:
        raise ValueError("HTML declarations are not supported")


def xml_document(data: bytes) -> ET.Element:
    """Parse bounded UTF-8 XML without DTD or entity declarations."""
    if len(data) > MAX_FILE:
        raise ValueError("Unsupported or oversized XML resource")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("XML resources must use UTF-8") from exc
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Unsupported XML resource")
    if re.search(r"<\?(?!xml[ \t\r\n])", text):
        raise ValueError("Processing instructions are not supported")
    return ET.fromstring(text)


def check_svg(data: bytes, *, exported: bool = False) -> ET.Element:
    """Refuse active content and external dependencies, including nested image data."""
    if exported:
        data = data.replace(SVG_DOCTYPE, b"", 1)
    root = xml_document(data)
    if root.tag != f"{{{SVG}}}svg":
        raise ValueError("Output is not SVG")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if not exported and (
            tag in {"text", "tspan", "tref", "textPath", "flowRoot", "flowPara", "flowSpan"}
            or tag.startswith("font")
        ):
            raise ValueError("Embedded SVG text and fonts are not supported")
        if (
            exported
            and tag == "a"
            and element.get("{http://www.w3.org/1999/xlink}href")
            == ("https://www.drawio.com/doc/faq/svg-export-text-problems")
        ):
            element.tag = f"{{{SVG}}}g"
            element.attrib.clear()
            tag = "g"
        if tag in {
            "script",
            "a",
            "iframe",
            "object",
            "embed",
            "audio",
            "video",
            "set",
            "mpath",
            "discard",
        } or tag.startswith(("animate", "animation")):
            raise ValueError("Active SVG content is not supported")
        if tag == "foreignObject" and not exported:
            raise ValueError("Embedded SVG HTML is not supported")
        for name, value in element.attrib.items():
            local = name.rsplit("}", 1)[-1]
            if local.startswith(("animation", "transition")) or (
                local == "style" and SVG_ANIMATION.search(value)
            ):
                raise ValueError("Active SVG CSS is not supported")
            if not exported and (
                local.startswith("font") or (local == "style" and SVG_FONT.search(value))
            ):
                raise ValueError("Embedded SVG text and fonts are not supported")
            if local.lower().startswith("on"):
                raise ValueError("SVG event handlers are not supported")
            if exported and (local, value) in {
                ("requiredFeatures", "http://www.w3.org/TR/SVG11/feature#Extensibility"),
                ("requiredExtensions", "http://www.w3.org/1999/xhtml"),
            }:
                continue
            if local in {"href", "src"}:
                if value.startswith("#"):
                    continue
                image_data(value)
            elif UNSAFE.search(value):
                if not re.fullmatch(r"url\(#[A-Za-z0-9_.:-]+\)", value):
                    raise ValueError(
                        f"SVG has an external or unsupported resource: {local}={value[:120]}"
                    )
        if tag == "style":
            css = "".join(element.itertext())
            if SVG_ANIMATION.search(css):
                raise ValueError("Active SVG CSS is not supported")
            if not exported and SVG_FONT.search(css):
                raise ValueError("Embedded SVG text and fonts are not supported")
            if UNSAFE.search(css):
                raise ValueError("SVG styles must not reference resources")
    return root


def image_data(value: str) -> str:
    """Validate an embedded image and return its data URI."""
    match = re.fullmatch(r"data:image/(png|jpeg|svg\+xml)(;base64)?,(.*)", value, re.S)
    if not match or len(value) > MAX_FILE:
        raise ValueError("Image must be a bounded embedded PNG, JPEG or SVG")
    try:
        data = (
            base64.b64decode(match[3], validate=True)
            if match[2]
            else urllib.parse.unquote_to_bytes(match[3])
        )
        if match[1] == "svg+xml":
            root = xml_document(data)
            if any(e.tag.rsplit("}", 1)[-1] in {"image", "feImage", "use"} for e in root.iter()):
                raise ValueError("Embedded SVG must not contain nested images or use elements")
            check_svg(data)
        else:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != {"png": "PNG", "jpeg": "JPEG"}[match[1]]:
                    raise ValueError("Image type does not match its data URI")
                dimensions(image.width, image.height)
                image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid embedded image") from exc
    return "data:image/" + match[1] + ";base64," + base64.b64encode(data).decode("ascii")


def dimensions(width: int, height: int) -> None:
    """Enforce the export's raster allocation budget."""
    if not 0 < width <= 4096 or not 0 < height <= 4096 or width * height > MAX_PIXELS:
        raise ValueError("Export exceeds the dimension or pixel limit")


def attribute_size(value: str) -> int:
    """Count UTF-8 bytes after ElementTree attribute escaping."""
    size = len(value.encode("utf-8"))
    for char, extra in (("&", 4), ("<", 3), (">", 3), ('"', 5), ("\r", 4), ("\n", 4), ("\t", 4)):
        size += value.count(char) * extra
    return size


class DocumentBudget:
    """Account for expanded XML before retaining replacement styles."""

    def __init__(self, root: ET.Element) -> None:
        self.size = 0
        ET.ElementTree(root).write(self, encoding="utf-8")

    def write(self, data: bytes) -> None:
        self.add(len(data))

    def add(self, size: int) -> None:
        self.size += size
        if self.size > MAX_PREPARED:
            raise ValueError("Prepared document exceeds the XML size limit")


def prepare_document(xml: str, manifest: dict) -> str:
    """Resolve images locally and normalize fonts without changing geometry."""
    root = xml_document(xml.encode())
    budget = DocumentBudget(root)
    verified_assets: dict[str, str] = {}
    for element in root.iter():
        if element.get("placeholders") == "1":
            raise ValueError("Placeholder labels are not supported by this export profile")
        if element.get("math") == "1":
            raise ValueError("Math labels are not supported by this export profile")
        for name, value in list(element.attrib.items()):
            if name in {"backgroundImage", "extFonts", "fontCss", "link", "image"}:
                raise ValueError("External/background resources and links are not supported")
            if name == "style":
                budget.add(-attribute_size(value))
                parts = re.split(r";(?=[A-Za-z_][\w.-]*=)", value.rstrip(";"))
                normalized: list[str] = []
                for part in parts:
                    key, sep, setting = part.partition("=")
                    if key in {"image", "indicatorImage"}:
                        if setting.startswith("https://app.diagrams.net/img/lib/"):
                            setting = setting.removeprefix("https://app.diagrams.net/")
                        asset = setting if setting in manifest["assets"] else None
                        if asset is not None and asset in verified_assets:
                            setting = verified_assets[asset]
                        elif asset is not None:
                            with (ROOT / "assets" / asset).open("rb") as file:
                                data = file.read(MAX_FILE + 1)
                            if len(data) > MAX_FILE:
                                raise ValueError("Runtime asset exceeds the image size limit")
                            if hashlib.sha256(data).hexdigest() != manifest["assets"][asset]:
                                raise RuntimeError("Runtime asset integrity check failed")
                            media = (
                                "svg+xml"
                                if setting.endswith(".svg")
                                else "png"
                                if setting.endswith(".png")
                                else "jpeg"
                            )
                            setting = (
                                "data:image/" + media + ";base64," + base64.b64encode(data).decode()
                            )
                        if asset is None or asset not in verified_assets:
                            if setting.startswith("data:image/") and ";base64," not in setting:
                                prefix, separator, payload = setting.partition(",")
                                if separator and re.fullmatch(r"[A-Za-z0-9+/=]+", payload):
                                    setting = prefix + ";base64," + payload
                            # The image getter restores mxGraph's reserved semicolon.
                            setting = image_data(setting).replace(";base64,", ",", 1)
                            if asset is not None:
                                verified_assets[asset] = setting
                    elif key == "fontFamily":
                        setting = FONT_ALIASES.get(setting, setting)
                        if setting not in manifest["fonts"]:
                            raise ValueError("Font is not in the offline font set")
                    elif UNSAFE.search(part):
                        raise ValueError("Style has an external or unsupported resource")
                    budget.add(attribute_size(key) + len(sep) + attribute_size(setting) + 1)
                    normalized.append(key + sep + setting)
                if element.tag == "mxCell" and not any(
                    p.startswith("fontFamily=") for p in normalized
                ):
                    budget.add(len("fontFamily=DejaVu Sans;"))
                    normalized.append("fontFamily=DejaVu Sans")
                element.set(name, ";".join(normalized) + ";")
            elif name in {"value", "label"}:
                label = Label(convert_charrefs=True)
                label.feed(value)
                label.close()
            elif UNSAFE.search(value):
                raise ValueError("Diagram metadata has an external or unsupported resource")
        if element.tag == "mxCell" and "style" not in element.attrib:
            budget.add(len(' style="fontFamily=DejaVu Sans;"'))
            element.set("style", "fontFamily=DejaVu Sans;")
    return ET.tostring(root, encoding="unicode")


def run_renderer(argv: list[str], deadline: float, profile: Path) -> None:
    """Bound diagnostics and retire the entire renderer group even after its leader exits."""
    environment = dict(
        os.environ, DRAWIO_DISABLE_UPDATE="true", HOME=str(profile), TMPDIR=str(profile)
    )
    identity = (
        {"user": RENDERER_ID, "group": RENDERER_ID, "extra_groups": []} if os.geteuid() == 0 else {}
    )
    diagnostic = bytearray()
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        cwd=profile,
        start_new_session=True,
        **identity,
    ) as process:

        def drain() -> None:
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                diagnostic.extend(chunk)
                del diagnostic[:-2048]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Renderer deadline exceeded: " + diagnostic.decode("utf-8", errors="replace")
            ) from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The process group may already have exited.
                pass
            process.wait()
            reader.join(timeout=2)
        if process.returncode:
            text = diagnostic.decode("utf-8", errors="replace")
            if "MAF_REFUSED:" in text:
                raise ValueError(text[text.index("MAF_REFUSED:") :][:256])
            raise RuntimeError("Native renderer failed: " + text[-1024:])


def renderer_directory(path: Path) -> None:
    """Give only renderer scratch directories to the unprivileged child."""
    path.mkdir(mode=0o700)
    if os.geteuid() == 0:
        os.chown(path, RENDERER_ID, RENDERER_ID)


def read_export(path: Path) -> bytes:
    """Read a bounded regular output without following renderer-created links."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_FILE:
            raise RuntimeError("Missing or oversized native export")
        data = file.read(MAX_FILE + 1)
    if not 0 < len(data) <= MAX_FILE:
        raise RuntimeError("Missing or oversized native export")
    return data


def export_document(xml: str, options: dict, deadline: float) -> None:
    """Render every requested page before publishing the output manifest."""
    try:
        manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid offline renderer manifest") from exc
    if manifest["version"] != 1 or manifest["desktop"] != "31.7.0":
        raise RuntimeError("Unsupported offline renderer manifest")
    if (
        hashlib.sha256(Path("/opt/drawio/resources/app.asar").read_bytes()).hexdigest()
        != manifest["asar_sha256"]
    ):
        raise RuntimeError("Renderer integrity check failed")
    prepared = prepare_document(xml, manifest)
    document = xml_document(prepared.encode())
    families = set()
    for element in document.iter():
        for declaration in element.get("style", "").split(";"):
            key, separator, value = declaration.partition("=")
            if key == "fontFamily" and separator:
                families.add(value)
    css = []
    for family in sorted(families):
        for font in manifest["font_variants"][family]:
            data = Path(font["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != font["sha256"]:
                raise RuntimeError("Font integrity check failed")
            css.append(
                f'@font-face{{font-family:"{family}";'
                f"font-weight:{font['weight']};font-style:{font['style']};"
                "src:url(data:font/ttf;base64," + base64.b64encode(data).decode() + ")}"
            )
    pages = len(document)
    selected = options["pages"] or list(range(1, pages + 1))
    if any(page > pages for page in selected):
        raise ValueError("Requested page is absent")
    names: list[str] = []
    total = len(xml.encode())
    with tempfile.TemporaryDirectory(prefix="drawio-", dir="/tmp") as directory:
        temporary = Path(directory)
        temporary.chmod(0o711)
        source = temporary / "input.drawio"
        source.write_text(prepared, "utf-8")
        source.chmod(0o644)
        for page in selected:
            for format in options["formats"]:
                name = f"diagram-{page}.{format}"
                output = temporary / f"output-{page}-{format}"
                renderer_directory(output)
                destination = output / name
                profile = temporary / f"profile-{page}-{format}"
                renderer_directory(profile)
                argv = [
                    "xvfb-run",
                    "-a",
                    "/opt/drawio/drawio",
                    "--disable-update",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--user-data-dir=" + str(profile),
                    "--export",
                    "--format",
                    format,
                    "--page-index",
                    str(page),
                    "--scale",
                    str(options["scale"]),
                    "--quality",
                    str(options["jpeg_quality"]),
                    "--embed-svg-images",
                    "--embed-svg-fonts",
                    "false",
                    "--theme",
                    "light",
                    "--output",
                    str(destination),
                    str(source),
                ]
                if options["transparent"] and format != "jpg":
                    argv.insert(-1, "--transparent")
                run_renderer(argv, deadline, profile)
                data = read_export(destination)
                if format == "svg":
                    root = check_svg(data, exported=True)
                    definitions = ET.SubElement(root, f"{{{SVG}}}defs")
                    ET.SubElement(definitions, f"{{{SVG}}}style").text = "\n".join(css)
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                else:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format != {"png": "PNG", "jpg": "JPEG"}[format]:
                            raise RuntimeError("Native output has the wrong image format")
                        dimensions(image.width, image.height)
                        image.load()
                size = len(data)
                total += size
                if size > MAX_FILE or total > MAX_TOTAL:
                    raise ValueError("Exports exceed the output byte limits")
                Path(name).write_bytes(data)
                names.append(name)
    Path("exports.json").write_text(json.dumps({"pages": pages, "files": names}), "utf-8")
