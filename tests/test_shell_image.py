"""Keep analyzer identities, verified component pins and release routing consistent."""

import json
import re
import runpy
from pathlib import Path

from maf_sandbox_shell._report import IDENTITY

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "images/shell-sandbox"
affected = runpy.run_path(str(ROOT / "scripts/select_image_security.py"))["affected"]


def test_recipe_and_kind_agree_on_analyzer_identities():
    recipe = json.loads((CONTEXT / "image.json").read_text())
    launcher = runpy.run_path(str(CONTEXT / "runner.py"))
    assert launcher["IDENTITY"] == IDENTITY
    for name in ("bash", "dash", "shellcheck", "powershell", "psscriptanalyzer"):
        assert recipe[name]["version"] == IDENTITY[name]
        assert re.fullmatch(r"[a-f0-9]{64}", recipe[name]["sha256"])
        assert recipe[name]["url"].startswith("https://")
    assert recipe["baseImage"] in (CONTEXT / "Dockerfile").read_text()


def test_shell_sources_select_only_the_shell_image():
    assert affected("images/shell-sandbox/runner.py") == ("shell",)
    assert affected("images/shell-sandbox/README.md") == ()
