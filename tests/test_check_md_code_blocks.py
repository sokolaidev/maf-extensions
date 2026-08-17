"""The markdown ```python-block linter, read against what it is meant to catch.

`scripts/check_md_code_blocks.py` answers one question: has a fenced ```python
block in the docs drifted from the `maf_sandbox*` API it shows a reader? It
extracts each block, runs pyright with everything off except `reportMissingImports`
and `reportAttributeAccessIssue`, and keeps only those two categories from the
output. The part that is easy to get wrong is the gate — a block that imports none
of the packages is a wiring fragment whose undefined `router`/`context` are the
host's, not drift, and top-level `await` is the noise of an illustrative snippet;
both must be skipped, not flagged. The part that is easy to *think* works but
doesn't is the drift catch itself, so it is exercised against a real pyright here
rather than asserted on faith ([[checks-that-cover-nothing]]).

The drift round-trips spawn pyright per block (the script does, internally), so
they are marked `slow` and can be skipped with `pytest -m "not slow"`. They stay
on by default — the whole point is that the gate runs in CI, which already runs
pyright.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_md_code_blocks", _ROOT / "scripts" / "check_md_code_blocks.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_ARGV0 = "scripts/check_md_code_blocks.py"

# A fenced block that imports a real export — pyright resolves it, the gate keeps
# the block, and nothing fires. This is the green baseline the drift cases stand
# against: a check that passed everything would pass this too, so alone it proves
# nothing; it only anchors the failing cases.
_GOOD_IMPORT = """# good

```python
from maf_sandbox import SandboxRouter

router = SandboxRouter([])
```
"""

# A fenced block that imports a name the package does not export. Pyright reports
# "is unknown import symbol" under `reportAttributeAccessIssue` — the rule the
# filter keeps — so this is the renamed-away-export case the issue is about.
_BAD_SYMBOL = """# bad symbol

```python
from maf_sandbox import SandboxRouter, DefinitelyNotExported

router = SandboxRouter([])
```
"""

# A fenced block that imports a module that does not exist. Pyright reports "could
# not be resolved" under `reportMissingImports`, and the line carries `maf_sandbox`
# (the fake module shares the prefix), so the filter keeps it too.
_BAD_MODULE = """# bad module

```python
import maf_sandbox_totally_fake

maf_sandbox_totally_fake.thing()
```
"""


class TestExtractingPythonBlocks:
    """Only ```python fences open a block; every other fence is passed over."""

    _MARKDOWN = """# title

```python
import os
```

```python3
import sys
```

```bash
pip install maf-sandbox
```

```
plain fence, not code
```

```python
x = 1
```
"""

    def test_it_returns_only_the_python_blocks(self, tmp_path: Path):
        from textwrap import dedent

        md = tmp_path / "sample.md"
        md.write_text(dedent(self._MARKDOWN))
        blocks = check.extract_python_code_blocks(str(md))
        # Three python blocks: `python`, `python3`, and the trailing `python`. The
        # `bash` and plain fences open nothing and so add nothing.
        assert len(blocks) == 3
        assert blocks[0][0].strip() == "import os"
        assert blocks[1][0].strip() == "import sys"
        assert blocks[2][0].strip() == "x = 1"

    def test_the_line_number_points_at_the_first_content_line(self, tmp_path: Path):
        md = tmp_path / "sample.md"
        md.write_text("line one\n\n```python\nfirst code line\nsecond code line\n```\n")
        blocks = check.extract_python_code_blocks(str(md))
        assert len(blocks) == 1
        _code, line_no = blocks[0]
        # The fence is line 3; the first content line is line 4.
        assert line_no == 4

    def test_a_non_python_fence_after_a_python_block_only_closes_it(self, tmp_path: Path):
        md = tmp_path / "sample.md"
        md.write_text("```python\nfoo\n```\n```bash\nbar\n```\n")
        blocks = check.extract_python_code_blocks(str(md))
        assert len(blocks) == 1
        assert blocks[0][0].strip() == "foo"

    def test_an_unclosed_block_at_eof_is_still_returned(self, tmp_path: Path):
        # A missing closing fence is malformed, but dropping the block would hide
        # its drift — so it is returned as if closed at end-of-file.
        md = tmp_path / "sample.md"
        md.write_text("```python\nfrom maf_sandbox import Gone\nx = Gone\n")
        blocks = check.extract_python_code_blocks(str(md))
        assert len(blocks) == 1
        assert "from maf_sandbox import Gone" in blocks[0][0]

    def test_a_nested_python_fence_closes_then_reopens(self, tmp_path: Path):
        # Two ```python fences with no close between them: the second closes the
        # first (preserving its content) and opens a new one, rather than resetting
        # and losing the first block.
        md = tmp_path / "sample.md"
        md.write_text("```python\nfirst\n```python\nsecond\n```\n")
        blocks = check.extract_python_code_blocks(str(md))
        assert len(blocks) == 2
        assert blocks[0][0].strip() == "first"
        assert blocks[1][0].strip() == "second"


class TestTheImportGate:
    """A block is checked iff it imports at least one `maf_sandbox*` package."""

    def test_a_block_importing_maf_sandbox_is_kept(self):
        assert check._imports_a_tracked_module("from maf_sandbox import SandboxRouter\n")

    def test_a_block_importing_a_dashed_package_is_kept(self):
        # `maf_sandbox_docker` contains `maf_sandbox` as a prefix; the gate must
        # not false-skip a block that imports a real sub-package.
        assert check._imports_a_tracked_module(
            "from maf_sandbox_docker import DockerSandboxBackend\n"
        )

    def test_a_block_importing_nothing_tracked_is_skipped(self):
        assert not check._imports_a_tracked_module(
            "router = SandboxRouter(backends)\nasync with router.scope(s, t) as r:\n    ...\n"
        )

    def test_a_bare_import_counts_as_well_as_a_from_import(self):
        assert check._imports_a_tracked_module("import maf_sandbox\n")

    def test_a_comment_mentioning_the_import_is_not_treated_as_one(self):
        # The gate anchors on `import`/`from` at line start, so a prose comment
        # that merely mentions `import maf_sandbox` does not satisfy it — the block
        # is skipped as the wiring fragment it is, not sent to pyright.
        assert not check._imports_a_tracked_module(
            "# import maf_sandbox for routing\nrouter = agent.run()\n"
        )

    def test_a_string_literal_mentioning_the_import_is_not_treated_as_one(self):
        assert not check._imports_a_tracked_module('x = "import maf_sandbox"\n')


@pytest.mark.slow
class TestTheDriftRoundTrip:
    """Real pyright, real installed packages — the proof the gate catches drift."""

    def test_a_real_export_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        md = tmp_path / "good.md"
        md.write_text(_GOOD_IMPORT)
        assert check.main([_ARGV0, str(md), "--no-glob"]) == 0
        assert "FAIL" not in capsys.readouterr().out

    def test_a_renamed_export_fails_with_an_attribute_access_issue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        md = tmp_path / "bad_symbol.md"
        md.write_text(_BAD_SYMBOL)
        assert check.main([_ARGV0, str(md), "--no-glob"]) == 1
        captured = capsys.readouterr()
        assert "reportAttributeAccessIssue" in captured.out
        assert "DefinitelyNotExported" in captured.out

    def test_a_missing_module_fails_with_a_missing_import(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        md = tmp_path / "bad_module.md"
        md.write_text(_BAD_MODULE)
        assert check.main([_ARGV0, str(md), "--no-glob"]) == 1
        captured = capsys.readouterr()
        assert "reportMissingImports" in captured.out
        assert "maf_sandbox_totally_fake" in captured.out

    def test_a_wiring_only_block_is_skipped_not_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Top-level `await` and an undefined `agent` — the noise the gate must
        # tolerate. No maf_sandbox* import, so it skips and stays green.
        md = tmp_path / "wiring.md"
        md.write_text(
            "# wiring\n\n```python\nsession = agent.create_session()\nfirst = await agent.run('hi', session=session)\n```\n"
        )
        assert check.main([_ARGV0, str(md), "--no-glob"]) == 0
        assert "SKIP" in capsys.readouterr().out


class TestTheCli:
    def test_no_files_matched_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0, str(tmp_path / "does-not-exist.md"), "--no-glob"]) == 2
        assert "no markdown files matched" in capsys.readouterr().err

    def test_an_exclude_substring_skips_a_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # A drift file that is excluded by substring never reaches pyright.
        drift = tmp_path / "drift_skipme.md"
        drift.write_text(_BAD_SYMBOL)
        # `--exclude skipme` matches the path, so the file is skipped and main exits 0.
        assert check.main([_ARGV0, str(drift), "--no-glob", "--exclude", "skipme"]) == 0
        assert "SKIP" in capsys.readouterr().out

    def test_the_script_runs_from_a_shell_in_any_cwd(self, tmp_path: Path):
        """`python scripts/...` is how the workflow calls it, from wherever it is.

        A wiring-only block (no `maf_sandbox*` import) is the right fixture here: it
        exercises arg parsing, file resolution and the SKIP path end to end without
        spawning pyright, so this stays out of the `slow` set the drift round-trips
        occupy.
        """
        wiring = tmp_path / "wiring.md"
        wiring.write_text(
            "# wiring\n\n```python\nsession = agent.create_session()\nfirst = await agent.run('hi')\n```\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "scripts" / "check_md_code_blocks.py"),
                str(wiring),
                "--no-glob",
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SKIP" in result.stdout


class TestFailClosed:
    """A pyright that does not run must never read as a clean pass.

    The `checks-that-cover-nothing` lesson: a gate that swallows a tool failure and
    reports OK passes drift silently. pyright that crashes or cannot start emits
    nothing on stdout and exits non-zero; the script must turn that into a FAIL,
    not the OK a genuine clean pass (exit 0, empty stdout) gets.
    """

    def test_a_pright_crash_with_no_stdout_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        md = tmp_path / "drift.md"
        md.write_text(_GOOD_IMPORT)  # would pass if pyright ran; the point is it does not

        class _Result:
            returncode = 2
            stdout = ""
            stderr = "pyright: internal error"

        monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: _Result())
        failures = check.check_code_blocks([str(md)], repo_root=tmp_path)
        assert failures == [f"{md}:4"]
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "pyright produced no output" in out
        assert "internal error" in out

    def test_a_genuine_clean_pass_still_reads_as_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        md = tmp_path / "good.md"
        md.write_text(_GOOD_IMPORT)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: _Result())
        failures = check.check_code_blocks([str(md)], repo_root=tmp_path)
        assert failures == []
        assert "OK" in capsys.readouterr().out

    def test_found_issues_pass_through_to_the_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        # exit non-zero *with* stdout is pyright's normal "found diagnostics" path,
        # not a crash — it must fall through to the relevant-error filter, which
        # here keeps the one attribute-access line.
        md = tmp_path / "drift.md"
        md.write_text(_BAD_SYMBOL)

        class _Result:
            returncode = 1
            stdout = (
                'snippet.py:1:40 - error: "DefinitelyNotExported" is unknown import symbol '
                "(reportAttributeAccessIssue)\n"
            )
            stderr = ""

        monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: _Result())
        failures = check.check_code_blocks([str(md)], repo_root=tmp_path)
        assert failures == [f"{md}:4"]
        assert "reportAttributeAccessIssue" in capsys.readouterr().out
