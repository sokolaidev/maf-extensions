"""Prepare the pinned Desktop archive and an offline resource manifest at installation."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

ROOT = Path("/opt/maf-drawio")
ASAR = Path("/opt/drawio/resources/app.asar")
WEB = "drawio/src/main/webapp/"


def prepare() -> None:
    """Add strict export checks and retain hashes for the installed renderer and assets."""
    data = ASAR.read_bytes()
    _, header_size, _, json_size = struct.unpack("<4I", data[:16])
    header = json.loads(data[16 : 16 + json_size])
    entries: list[tuple[str, dict]] = []

    def visit(tree: dict, prefix: str = "") -> None:
        for name, item in tree.items():
            path = prefix + name
            if "files" in item:
                visit(item["files"], path + "/")
            elif "offset" in item:
                entries.append((path, item))

    visit(header["files"])
    guard = (ROOT / "guard.js").read_text("utf-8")
    payload = bytearray()
    assets: dict[str, str] = {}
    patched = False
    for name, item in entries:
        start = 8 + header_size + int(item["offset"])
        content = data[start : start + item["size"]]
        if name == WEB + "js/export.js":
            source = content.decode("utf-8")
            if "cache[src].onerror = decrementWaitCounter;" not in source:
                raise ValueError("Unsupported Draw.io image-loading contract")
            source = source.replace(
                "cache[src].onerror = decrementWaitCounter;",
                (
                    "cache[src].onerror = function() { mafFailure = 'Image failed to load'; "
                    "decrementWaitCounter(); };"
                ),
            ).replace("electron.sendMessage(", "mafSend(")
            content = (source + "\n" + guard).encode()
            patched = True
        if name.startswith(WEB + "img/lib/") and name.endswith((".svg", ".png", ".jpg")):
            relative = name.removeprefix(WEB)
            destination = ROOT / "assets" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            assets[relative] = hashlib.sha256(content).hexdigest()
        item["offset"] = str(len(payload))
        item["size"] = len(content)
        block_size = 4 * 1024 * 1024
        item["integrity"] = {
            "algorithm": "SHA256",
            "hash": hashlib.sha256(content).hexdigest(),
            "blockSize": block_size,
            "blocks": [
                hashlib.sha256(content[i : i + block_size]).hexdigest()
                for i in range(0, len(content), block_size)
            ],
        }
        payload.extend(content)
    if not patched or not assets:
        raise ValueError("Pinned Desktop resources were not found")
    raw = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    padded = raw + b"\0" * (-len(raw) % 4)
    new_header = struct.pack("<II", len(padded) + 4, len(raw)) + padded
    ASAR.write_bytes(struct.pack("<II", 4, len(new_header)) + new_header + payload)
    font_root = Path("/usr/share/fonts/truetype/dejavu")
    if not font_root.is_dir():
        font_root = Path("/usr/share/fonts/ttf-dejavu")
    fonts = {
        "DejaVu Sans": str(font_root / "DejaVuSans.ttf"),
        "DejaVu Serif": str(font_root / "DejaVuSerif.ttf"),
        "DejaVu Sans Mono": str(font_root / "DejaVuSansMono.ttf"),
    }
    variants = {}
    for family, regular in fonts.items():
        italic = "Italic" if family == "DejaVu Serif" else "Oblique"
        variants[family] = []
        for suffix, weight, style in (
            ("", 400, "normal"),
            ("-Bold", 700, "normal"),
            ("-" + italic, 400, "italic"),
            ("-Bold" + italic, 700, "italic"),
        ):
            path = Path(regular.replace(".ttf", suffix + ".ttf"))
            variants[family].append(
                {
                    "path": str(path),
                    "weight": weight,
                    "style": style,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    manifest = {
        "version": 1,
        "desktop": "31.7.0",
        "asar_sha256": hashlib.sha256(ASAR.read_bytes()).hexdigest(),
        "assets": assets,
        "fonts": fonts,
        "font_variants": variants,
        "font_hashes": {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in fonts.items()
        },
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), "utf-8")


if __name__ == "__main__":
    prepare()
