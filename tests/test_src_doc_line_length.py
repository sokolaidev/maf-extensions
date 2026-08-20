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

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
# packages/*/pyproject.toml, not "maf-sandbox*": a workspace package that does not
# mirror the sandbox naming is still subject to the length rules. Same discovery as
# scripts/gate_tasks.py and tests/test_release_config.py.
_PACKAGES = sorted(_REPO_ROOT.glob("packages/*/pyproject.toml"))

_SELECT = "E501,W505"
_MAX_DOC = 100
_LENGTH_RULES = ("E501", "W505")


def _selects_length_rule(rule: str) -> bool:
    """Whether a ruff selector names either length rule.

    Selectors are prefixes (``E5`` matches ``E501``) and ``ALL`` matches everything,
    so an exact-membership test would miss both.
    """
    if rule == "ALL":
        return True
    return any(target.startswith(rule) for target in _LENGTH_RULES)


def src_pattern_matches(src: Path, pattern: str) -> bool:
    """Whether a ruff path pattern could resolve to a ``src/`` file.

    Ruff's ``exclude``/``per-file-ignores`` patterns are globs over the project root
    (the package dir that holds ``pyproject.toml``), so they match ``src/x.py``, not
    ``x.py``. A leading ``!`` negates the pattern and is unprovable by fnmatch.
    """
    if pattern.startswith("!"):
        return True
    return any(
        fnmatch.fnmatch(path.relative_to(src.parent).as_posix(), pattern)
        for path in src.rglob("*.py")
    )


def _src_ignores(src: Path, lint: dict) -> list[str]:
    """Per-file-ignore entries that silence ``E501``/``W505`` on ``src/`` files."""
    offenders: list[str] = []
    for pattern, rules in lint.get("per-file-ignores", {}).items():
        if not any(_selects_length_rule(rule) for rule in rules):
            continue
        if pattern.startswith("!") or src_pattern_matches(src, pattern):
            # A negated pattern ("!tests/**") makes ruff apply the listed rules only
            # outside the named glob; its effect on src/ cannot be proven, and it
            # disables them on every other file. Reject it fail-closed.
            offenders.append(f"{pattern} -> {sorted(rules)}")
    return offenders


def _assert_enforced(pyproject: Path) -> None:
    """The package's own config enables the length rules, or the scan proves nothing.

    A ruff run proves src/ is clean today; it cannot prove a dropped select, a missing
    ``max-doc-length`` (which makes ``W505`` a no-op), a raised ``line-length``, an exclude
    covering ``src/``, or an ignore matching the rules out of ``src/`` was ever enforced.
    All of those are rejected here.
    """
    config = tomllib.loads(pyproject.read_text())
    tool = config["tool"]["ruff"]
    lint = tool["lint"]
    package = pyproject.parent
    assert tool.get("line-length") == _MAX_DOC, (
        f"{package.name} dropped line-length={_MAX_DOC}; E501 no longer enforces the "
        f"100-column rule (#454)"
    )
    selected = list(lint.get("select", [])) + list(lint.get("extend-select", []))
    assert "E501" in selected and "W505" in selected, (
        f"{package.name} dropped the line-length rules (#454)"
    )
    assert lint.get("pycodestyle", {}).get("max-doc-length") == _MAX_DOC, (
        f"{package.name} dropped max-doc-length={_MAX_DOC}; W505 is a no-op without it (#454)"
    )
    for key in ("ignore", "extend-ignore"):
        disabled = [rule for rule in lint.get(key, []) if _selects_length_rule(rule)]
        assert not disabled, (
            f"{package.name} {key}={disabled} disables the line-length rules (#454)"
        )
    src = package / "src"
    # `exclude`/`extend-exclude` are honoured during directory traversal, not for the
    # explicit `src/` path the scan passes below, so a lint-scoped one is only a trap for
    # a future dir-scan. The top-level `[tool.ruff]` lists, however, hold through: they
    # are read before lint settings and would hide `src/` from the scan even now. Reject
    # both, and reject both scopes fail-closed.
    for table, key in (
        (lint, "exclude"),
        (lint, "extend-exclude"),
        (tool, "exclude"),
        (tool, "extend-exclude"),
    ):
        for pattern in table.get(key, []):
            if src_pattern_matches(src, pattern):
                raise AssertionError(
                    f"{package.name} {key}={pattern!r} could exclude src/ files "
                    f"from the length rules (#454)"
                )
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
        "enforcing them (#454).\n" + result.stdout + result.stderr
    )


class TestSrcDocLineLength:
    def test_every_package_src_is_clean(self):
        assert _PACKAGES, "no packages/*/pyproject.toml found"
        for pyproject in _PACKAGES:
            _assert_enforced(pyproject)
            _assert_clean(pyproject)

    def _tree(self, tmp_path: Path) -> Path:
        src = tmp_path / "pkg" / "src"
        src.mkdir(parents=True)
        (src / "x.py").write_text("x = 1\n", encoding="utf-8")
        return src

    def test_src_pattern_matches_shrinks_src_prefix(self, tmp_path):
        # `exclude = ["src/**"]` is a glob over the package dir (the scan root's
        # parent), so it must be caught even though the scan root is itself src/.
        src = self._tree(tmp_path)
        assert src_pattern_matches(src, "src/**")

    def test_src_pattern_matches_misses_unrelated(self, tmp_path):
        src = self._tree(tmp_path)
        assert not src_pattern_matches(src, "tests/**")

    def test_src_ignores_rejects_negated_length_rule(self):
        lint = {"per-file-ignores": {"!tests/**": ["E501", "W505"]}}
        assert _src_ignores(Path("pkg/src"), lint)

    def test_src_ignores_accepts_unrelated_rules(self):
        lint = {"per-file-ignores": {"src/**": ["F401"]}}
        assert _src_ignores(Path("pkg/src"), lint) == []

    def test_assert_enforced_rejects_exclude_shadowing_src(self, tmp_path):
        # `exclude = ["src/**"]` is honoured during directory traversal; a scan that
        # passes a directory instead of explicit files would let it silence the gate.
        pkg = tmp_path / "pkg"
        (pkg / "src").mkdir(parents=True)
        (pkg / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        pyproject = pkg / "pyproject.toml"
        pyproject.write_text(
            """\
[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E501", "W505"]
exclude = ["src/**"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 100
""",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="could exclude src/ files"):
            _assert_enforced(pyproject)

    def test_assert_enforced_rejects_top_level_exclude(self, tmp_path):
        # `[tool.ruff] exclude` is read before lint settings, so it holds even for the
        # explicit src/ path the scan passes; the guard must reject it.
        pkg = tmp_path / "pkg"
        (pkg / "src").mkdir(parents=True)
        (pkg / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        pyproject = pkg / "pyproject.toml"
        pyproject.write_text(
            """\
[tool.ruff]
line-length = 100
exclude = ["src/**"]

[tool.ruff.lint]
select = ["E501", "W505"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 100
""",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="could exclude src/ files"):
            _assert_enforced(pyproject)

    def test_assert_enforced_rejects_line_length_above_100(self, tmp_path):
        # E501 reads the configured line-length, so it must stay at the 100-column floor;
        # a higher line-length would let code drift past what the doc rule bounds.
        pkg = tmp_path / "pkg"
        (pkg / "src").mkdir(parents=True)
        (pkg / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        pyproject = pkg / "pyproject.toml"
        pyproject.write_text(
            """\
[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E501", "W505"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 100
""",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="line-length=100"):
            _assert_enforced(pyproject)

    def test_src_ignores_rejects_length_rule_prefix(self, tmp_path):
        # Selectors are prefixes ("E5") and "ALL", and either must be rejected fail-closed.
        src = tmp_path / "pkg" / "src"
        src.mkdir(parents=True)
        (src / "x.py").write_text("x = 1\n", encoding="utf-8")
        assert _src_ignores(src, {"per-file-ignores": {"src/**": ["E5"]}})
        assert _src_ignores(src, {"per-file-ignores": {"src/**": ["ALL"]}})

    def test_src_ignores_accepts_unrelated_prefix(self):
        # A prefix that names no length rule (e.g. "F5") is not a length-rule ignore.
        lint = {"per-file-ignores": {"src/**": ["F5"]}}
        assert _src_ignores(Path("pkg/src"), lint) == []

    def test_assert_enforced_rejects_ignore_prefix(self, tmp_path):
        # `ignore`/`extend-ignore` follow the same selector semantics as per-file-ignores:
        # a prefix ("E5") or "ALL" still disables the length rules fail-closed.
        pkg = tmp_path / "pkg"
        (pkg / "src").mkdir(parents=True)
        (pkg / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        pyproject = pkg / "pyproject.toml"
        pyproject.write_text(
            """\
[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E501", "W505"]
ignore = ["E5"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 100
""",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="disables the line-length rules"):
            _assert_enforced(pyproject)

    def test_assert_clean_failure_appends_stderr(self, tmp_path, monkeypatch):
        # A failed scan must surface stderr too, or a violation that only appears there
        # (a config error, say) is invisible in the assertion message.
        src = tmp_path / "pkg" / "src"
        src.mkdir(parents=True)
        (src / "x.py").write_text("x = 1\n", encoding="utf-8")
        pyproject = tmp_path / "pkg" / "pyproject.toml"
        pyproject.write_text("", encoding="utf-8")

        class R:
            returncode = 1
            stdout = "out-line\n"
            stderr = "err-line\n"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        with pytest.raises(AssertionError, match="err-line"):
            _assert_clean(pyproject)
