"""The pre-flight that keeps a live sample off a version its own uv cannot yet see (#595).

The failure it exists for is silent in the direction that matters: the sample passes, the run
is green, and what it measured was the previous release.

The first version of this pre-flight polled the index with `urllib` and was pinned by tests
that checked the parsing of an index document — every one of them green while the thing shipped
did not work. So these assert on the *probe*: that it is `uv`, that it names the exact version,
and that it refuses a cached answer.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "await_live_version.py"

_spec = importlib.util.spec_from_file_location("await_live_version", _SCRIPT)
assert _spec and _spec.loader
await_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(await_live)


def _done(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class TestTheProbeIsUvItself:
    """A different HTTP client reads a different CDN cache object, so only uv answers for uv."""

    def test_the_probe_runs_uv(self):
        assert await_live.command("maf-sandbox-docker", "0.7.2")[0] == "uv"

    def test_it_runs_the_subcommand_the_samples_run(self):
        """`uv run` resolves differently enough from `uv pip` to be worth pinning."""
        assert await_live.command("p", "1.0")[1] == "run"

    def test_it_names_the_exact_version(self):
        """Without `==`, uv resolves the newest the edge offers and the probe proves nothing."""
        assert "--with" in await_live.command("maf-sandbox-docker", "0.7.2")
        assert "maf-sandbox-docker==0.7.2" in await_live.command("maf-sandbox-docker", "0.7.2")

    def test_it_refuses_uvs_cached_answer(self):
        """A cached miss would otherwise let the loop spin against its own memory to the deadline."""
        assert "--refresh" in await_live.command("p", "1.0")

    def test_it_does_not_read_the_repository_it_stands_in(self):
        """The harness checkout is not a project, and a stray pyproject.toml would change the run."""
        assert "--no-project" in await_live.command("p", "1.0")


class TestWhatTheProbeReports:
    def test_a_zero_exit_is_a_pass(self):
        assert await_live.probe("p", "1.0", run=lambda *a, **k: _done(0)) is None

    def test_a_failure_carries_uvs_own_words(self):
        reason = await_live.probe(
            "p", "1.0", run=lambda *a, **k: _done(1, "  because there is no version of p==1.0\n")
        )
        assert reason == "because there is no version of p==1.0"

    def test_a_wrapped_complaint_arrives_whole(self):
        """uv wraps one sentence over four lines; the last of them is the word "unsatisfiable"."""
        stderr = (
            "  \x1b[31m\u00d7\x1b[0m No solution found when resolving `--with` dependencies:\n"
            "\x1b[31m  \u2570\u2500\u25b6 \x1b[0mBecause there is no version of p==1.0 and you\n"
            "\x1b[31m      \x1b[0mrequire p==1.0, we can conclude that your requirements are\n"
            "\x1b[31m      \x1b[0munsatisfiable.\n"
        )
        reason = await_live.probe("p", "1.0", run=lambda *a, **k: _done(1, stderr))
        assert reason.startswith("No solution found"), reason
        assert "there is no version of p==1.0" in reason
        assert "\x1b" not in reason, "the colour codes reached the log"

    def test_a_very_long_complaint_is_cut_rather_than_flooding_the_log(self):
        reason = await_live.probe("p", "1.0", run=lambda *a, **k: _done(1, "word " * 400))
        assert len(reason) <= 300

    def test_a_failure_with_nothing_to_say_still_says_something(self):
        assert await_live.probe("p", "1.0", run=lambda *a, **k: _done(2)) == "uv exited 2"

    def test_uvs_advice_lines_are_not_the_reason(self):
        """uv ends with `help:`/`hint:`; quoting those loses the sentence that names the fault."""
        stderr = "no version of p==1.0 is available\nhint: try another version\n"
        reason = await_live.probe("p", "1.0", run=lambda *a, **k: _done(1, stderr))
        assert reason == "no version of p==1.0 is available"

    def test_uvs_output_is_decoded_as_the_utf_8_it_writes(self):
        """`text=True` would use the locale encoding, and the glyph stripping below silently
        stops working the moment the bytes arrive mojibake."""
        seen = {}

        def record(*_args, **kwargs):
            seen.update(kwargs)
            return _done(0)

        await_live.probe("p", "1.0", run=record)
        assert seen.get("encoding") == "utf-8", seen
        assert not seen.get("text"), "text=True defeats the explicit encoding"

    def test_a_hung_uv_is_a_reason_rather_than_a_hang(self):
        def hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=await_live.ATTEMPT_TIMEOUT_SECONDS)

        assert "did not answer" in (await_live.probe("p", "1.0", run=hang) or "")

    def test_a_missing_uv_is_a_reason_rather_than_a_traceback(self):
        def absent(*_args, **_kwargs):
            raise FileNotFoundError("uv")

        assert "could not run uv" in (await_live.probe("p", "1.0", run=absent) or "")


class _Clock:
    """A monotonic clock that only moves when the code under test sleeps.

    It refuses to run away. A wait that loses its deadline loops for ever, and a test that
    hangs reports as a job timeout twenty minutes later rather than as the failure it is.
    """

    #: Far past the sixty sleeps a real wait takes, so only a lost deadline reaches it.
    RUNAWAY = 1000

    def __init__(self) -> None:
        self.at = 0.0
        self.slept = 0

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept += 1
        if self.slept > self.RUNAWAY:
            raise AssertionError("the wait never ends; it has slept past any deadline")
        self.at += seconds


class TestTheWait:
    def test_a_version_uv_already_sees_costs_no_sleep(self):
        clock = _Clock()
        status = await_live.await_version(
            "p", "1.0", attempt=lambda *_: None, now=clock.now, sleep=clock.sleep
        )
        assert status == 0
        assert clock.at == 0.0, "a resolvable version must not delay the sample"

    def test_an_edge_that_catches_up_is_waited_for(self):
        clock = _Clock()
        answers = iter(["not listed", "not listed", None])
        status = await_live.await_version(
            "p", "1.0", attempt=lambda *_: next(answers), now=clock.now, sleep=clock.sleep
        )
        assert status == 0
        assert clock.at == 2 * await_live.INTERVAL_SECONDS

    def test_one_that_never_does_fails_rather_than_running_the_sample(self):
        clock = _Clock()
        status = await_live.await_version(
            "p", "1.0", attempt=lambda *_: "not listed", now=clock.now, sleep=clock.sleep
        )
        assert status == 1
        assert clock.at >= await_live.DEADLINE_SECONDS

    def test_it_stops_rather_than_running_past_the_deadline(self):
        """The job has its own timeout, and being killed by it reports nothing a reader can use."""
        clock = _Clock()
        await_live.await_version(
            "p", "1.0", attempt=lambda *_: "not listed", now=clock.now, sleep=clock.sleep
        )
        assert clock.at < await_live.DEADLINE_SECONDS + await_live.INTERVAL_SECONDS


class TestTheCli:
    def test_it_refuses_the_wrong_argument_count(self):
        assert await_live.main(["await_live_version.py", "maf-sandbox-docker"]) == 2
