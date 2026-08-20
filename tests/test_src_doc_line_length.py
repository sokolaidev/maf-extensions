"""Pin that no package's shipped `src/` carries an over-limit code or prose line.

Each package selects `E501` and `W505` (at `max-doc-length = 100`) in its own
`pyproject.toml`, so a reverted select, a dropped `max-doc-length`, an ignore that
matches the length rules out of `src/`, or a new over-limit line all fail here (#454).
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# packages/*/pyproject.toml, not "maf-sandbox*": a workspace package that does not
# mirror the sandbox naming is still subject to the length rules. Same discovery as
# scripts/gate_tasks.py and tests/test_release_config.py.
_PACKAGES = sorted(_REPO_ROOT.glob("packages/*/pyproject.toml"))

_SELECT = "E501,W505"
_MAX_DOC = 100


def _src_ignores(src: Path, lint: dict) -> list[str]:
    """Per-file-ignore entries that silence ``E501``/``W505`` on ``src/`` files."""
    offenders: list[str] = []
    for pattern, rules in lint.get("per-file-ignores", {}).items():
        if not any(rule in rules for rule in ("E501", "W505")):
            continue
        if any(
            fnmatch.fnmatch(path.relative_to(src.parent).as_posix(), pattern)
            for path in src.rglob("*.py")
        ):
            offenders.append(f"{pattern} -> {sorted(rules)}")
    return offenders


def _assert_enforced(pyproject: Path) -> None:
    """The package's own config enables the length rules, or the scan proves nothing.

    A ruff run proves src/ is clean *today*; it cannot prove a dropped select, a missing
    ``max-doc-length`` (which makes ``W505`` a no-op), or an ignore matching the rules out
    of ``src/`` was ever enforced. All of those are rejected here.
    """
    config = tomllib.loads(pyproject.read_text())
    lint = config["tool"]["ruff"]["lint"]
    package = pyproject.parent
    selected = list(lint.get("select", [])) + list(lint.get("extend-select", []))
    assert "E501" in selected and "W505" in selected, (
        f"{package.name} dropped the line-length rules (#454)"
    )
    assert lint.get("pycodestyle", {}).get("max-doc-length") == _MAX_DOC, (
        f"{package.name} dropped max-doc-length={_MAX_DOC}; W505 is a no-op without it (#454)"
    )
    for key in ("ignore", "extend-ignore"):
        disabled = [rule for rule in lint.get(key, []) if rule in ("E501", "W505")]
        assert not disabled, (
            f"{package.name} {key}={disabled} disables the line-length rules (#454)"
        )
    src = package / "src"
    ignores = _src_ignores(src, lint)
    assert not ignores, (
        f"{package.name} per-file-ignores {_SELECT} out of src/: {', '.join(ignores)} (#454)"
    )


def _assert_clean(pyproject: Path) -> None:
    package = pyproject.parent
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
        assert _PACKAGES, "no packages/*/pyproject.toml found"
        for pyproject in _PACKAGES:
            _assert_enforced(pyproject)
            _assert_clean(pyproject)
