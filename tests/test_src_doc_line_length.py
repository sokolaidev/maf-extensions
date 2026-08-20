"""Pin that no package's shipped `src/` carries an over-limit code or prose line.

`AGENTS.md` asks for short comments and docstrings, and issue #454's stage one
makes `E501` (line-length) and `W505` (max-doc-length) enforced rules in every
package's `pyproject.toml`. The per-file-ignores grandfather the `tests/`,
`scripts/` and `samples/` trees out of the prose rules — `src/` is what a reader
sees, so it is where the rules must actually bite. This test is the backstop:
a rule reverted, a `max-doc-length` dropped, or a new over-limit line added to
`src/` all fail here. The first two are caught up front — `W505` is a no-op
without `max-doc-length`, and a clean scan cannot prove an unenforced rule was
ever selected in the first place.

Each package is checked through its own `pyproject.toml` (ruff resolves the
nearest ancestor per file), so the package's `max-doc-length` and per-file-ignores
apply exactly as they do in the gate.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGES = sorted((_REPO_ROOT / "packages").glob("maf-sandbox*"))

_SELECT = "E501,W505"
_MAX_DOC = 100


def _assert_enforced(package: Path) -> None:
    """The package's own config enables the length rules, or the scan proves nothing.

    A ruff run proves src/ is clean *today*; it cannot prove a dropped select or a
    missing ``max-doc-length`` (which makes ``W505`` a no-op) was ever enforced in the
    first place.
    """
    config = tomllib.loads((package / "pyproject.toml").read_text())
    lint = config["tool"]["ruff"]["lint"]
    selected = list(lint.get("select", [])) + list(lint.get("extend-select", []))
    assert "E501" in selected and "W505" in selected, (
        f"{package.name} dropped the line-length rules (#454)"
    )
    assert lint.get("pycodestyle", {}).get("max-doc-length") == _MAX_DOC, (
        f"{package.name} dropped max-doc-length={_MAX_DOC}; W505 is a no-op without it (#454)"
    )


def _assert_clean(package: Path) -> None:
    src = package / "src"
    assert src.is_dir(), f"{package.name} has no src/ tree"

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--select", _SELECT, str(src)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{package.name} src/ violates {_SELECT}. The package's pyproject enables these rules; "
        "a violation here is either a new over-limit line or a config that silently stopped "
        "enforcing them (#454).\n" + result.stdout
    )


class TestSrcDocLineLength:
    def test_every_package_src_is_clean(self):
        assert _PACKAGES, "no packages/maf-sandbox* found"
        for package in _PACKAGES:
            _assert_enforced(package)
            _assert_clean(package)
