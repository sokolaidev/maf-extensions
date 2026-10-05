"""Check the complete bundled Azure SVG collection against the export policy."""

import runpy
from pathlib import Path


def check_assets(root: Path) -> tuple[int, int]:
    """Check every icon, retaining the known Private Endpoint font-policy refusal."""
    check_svg = runpy.run_path(str(root / "export.py"))["check_svg"]
    assets = sorted((root / "assets/img/lib/azure2").rglob("*.svg"))
    if not assets:
        raise ValueError("Bundled Azure SVG icons are missing")
    refused = 0
    for asset in assets:
        try:
            check_svg(asset.read_bytes())
        except ValueError as exc:
            if (
                asset.relative_to(root).as_posix()
                == "assets/img/lib/azure2/networking/Private_Endpoint.svg"
                and str(exc) == "Embedded SVG text and fonts are not supported"
            ):
                refused += 1
                continue
            raise ValueError(f"{asset.relative_to(root)}: {exc}") from exc
    return len(assets) - refused, refused


if __name__ == "__main__":
    accepted, refused = check_assets(Path("/opt/maf-drawio"))
    print(f"Azure SVG icons: {accepted} accepted, {refused} known font-policy refusals")
