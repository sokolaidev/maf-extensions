"""Inventory the installed native export runtime and its retained notices inside the guest."""

import hashlib
import json
import struct
from pathlib import Path


def inventory() -> dict[str, object]:
    """Bind versions, renderer bytes, bundled assets and license notices to the observed image."""
    root = Path("/opt/maf-drawio")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    archive = Path("/opt/drawio/resources/app.asar").read_bytes()
    _, header_size, _, json_size = struct.unpack("<4I", archive[:16])
    header = json.loads(archive[16 : 16 + json_size])
    entry = header["files"]["package.json"]
    start = 8 + header_size + int(entry["offset"])
    package = json.loads(archive[start : start + entry["size"]])
    notices = {}
    for name in (
        "/opt/drawio/LICENSE.electron.txt",
        "/opt/drawio/LICENSES.chromium.html",
        "/opt/maf-drawio/notices/desktop-LICENSE",
        "/opt/maf-drawio/notices/drawio-LICENSE",
        "/usr/share/licenses/drawio/DEJAVU-LICENSE",
    ):
        content = Path(name).read_bytes()
        if not content:
            raise ValueError("Missing native runtime notice")
        notices[name] = hashlib.sha256(content).hexdigest()
    if package["version"] != manifest["desktop"]:
        raise ValueError("Installed Desktop version differs from runtime manifest")
    packages = []
    for relative, expected in manifest["package_inventory"].items():
        metadata = (root / "inventory" / relative).read_bytes()
        if hashlib.sha256(metadata).hexdigest() != expected:
            raise ValueError("Installed package inventory differs from its manifest")
        item = json.loads(metadata)
        packages.append({"name": item["name"], "version": item["version"]})
    return {
        "desktop": package["version"],
        "electron": manifest["electron"],
        "electronVersionSource": manifest["electron_version_source"],
        "asarSha256": hashlib.sha256(archive).hexdigest(),
        "rendererSha256": hashlib.sha256(Path("/opt/drawio/drawio").read_bytes()).hexdigest(),
        "assetCount": len(manifest["assets"]),
        "fontFamilies": sorted(manifest["fonts"]),
        "nativeNotices": notices,
        "packageInventory": packages,
    }


if __name__ == "__main__":
    print(json.dumps(inventory()))
