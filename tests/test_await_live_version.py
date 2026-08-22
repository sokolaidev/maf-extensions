"""The pre-flight that keeps a live sample off a stale PyPI edge (#595).

The failure it exists for is silent in the direction that matters: the sample passes, the run
is green, and what it measured was the previous release.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "await_live_version.py"

_spec = importlib.util.spec_from_file_location("await_live_version", _SCRIPT)
assert _spec and _spec.loader
await_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(await_live)


def _index(*filenames: str, versions: tuple[str, ...] = ()) -> dict:
    return {
        "versions": list(versions),
        "files": [{"filename": name} for name in filenames],
    }


def _served() -> dict:
    return _index("maf_sandbox_docker-0.7.2-py3-none-any.whl", versions=("0.7.1", "0.7.2"))


def _stale() -> dict:
    return _index("maf_sandbox_docker-0.7.1-py3-none-any.whl", versions=("0.7.1",))


class TestWhatCountsAsServed:
    def test_the_version_listed_with_its_wheel_is_served(self):
        assert await_live.serves(_served(), "maf-sandbox-docker", "0.7.2") is None

    def test_an_edge_that_has_not_caught_up_is_not(self):
        assert await_live.serves(_stale(), "maf-sandbox-docker", "0.7.2") == "version not listed"

    def test_listed_without_a_wheel_is_not_served_either(self):
        """The listing and the artifacts appear independently, so one is not evidence of both."""
        payload = _index("maf_sandbox_docker-0.7.2.tar.gz", versions=("0.7.2",))
        reason = await_live.serves(payload, "maf-sandbox-docker", "0.7.2")
        assert reason == "listed, but no wheel for it yet"

    def test_the_wheel_is_matched_on_the_module_name(self):
        """PyPI names the file after the module, so matching the distribution finds nothing."""
        payload = _index("maf-sandbox-docker-0.7.2-py3-none-any.whl", versions=("0.7.2",))
        assert await_live.serves(payload, "maf-sandbox-docker", "0.7.2") is not None

    def test_another_versions_wheel_does_not_satisfy_this_one(self):
        payload = _index("maf_sandbox_docker-0.7.21-py3-none-any.whl", versions=("0.7.2",))
        assert await_live.serves(payload, "maf-sandbox-docker", "0.7.2") is not None


class _Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.at = 0.0

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


class TestTheWait:
    def test_an_edge_already_serving_it_costs_no_sleep(self):
        clock = _Clock()
        status = await_live.await_version(
            "maf-sandbox-docker",
            "0.7.2",
            fetch=lambda _: _served(),
            now=clock.now,
            sleep=clock.sleep,
        )
        assert status == 0
        assert clock.at == 0.0, "a served edge must not delay the sample"

    def test_an_edge_that_catches_up_is_waited_for(self):
        clock = _Clock()
        reads = iter([_stale(), _stale(), _served()])
        status = await_live.await_version(
            "maf-sandbox-docker",
            "0.7.2",
            fetch=lambda _: next(reads),
            now=clock.now,
            sleep=clock.sleep,
        )
        assert status == 0
        assert clock.at == 2 * await_live.INTERVAL_SECONDS

    def test_an_edge_that_never_catches_up_fails_rather_than_running_the_sample(self):
        clock = _Clock()
        status = await_live.await_version(
            "maf-sandbox-docker",
            "0.7.2",
            fetch=lambda _: _stale(),
            now=clock.now,
            sleep=clock.sleep,
        )
        assert status == 1
        assert clock.at >= await_live.DEADLINE_SECONDS

    def test_it_fails_rather_than_hanging_past_the_deadline(self):
        """The job has its own timeout, and being killed by it reports nothing a reader can use."""
        clock = _Clock()
        await_live.await_version(
            "maf-sandbox-docker",
            "0.7.2",
            fetch=lambda _: _stale(),
            now=clock.now,
            sleep=clock.sleep,
        )
        assert clock.at < await_live.DEADLINE_SECONDS + await_live.INTERVAL_SECONDS

    def test_an_unreachable_index_is_retried_rather_than_fatal(self):
        """A refused connection mid-propagation is the condition, not a reason to stop."""
        clock = _Clock()
        reads = iter([urllib.error.URLError("refused"), _served()])

        def fetch(_package):
            answer = next(reads)
            if isinstance(answer, Exception):
                raise answer
            return answer

        status = await_live.await_version(
            "maf-sandbox-docker", "0.7.2", fetch=fetch, now=clock.now, sleep=clock.sleep
        )
        assert status == 0

    def test_half_a_document_is_retried_rather_than_fatal(self):
        """An edge mid-write answers 200 with truncated JSON; that is what is being waited out."""
        clock = _Clock()
        reads = iter([json.JSONDecodeError("truncated", "", 0), _served()])

        def fetch(_package):
            answer = next(reads)
            if isinstance(answer, Exception):
                raise answer
            return answer

        status = await_live.await_version(
            "maf-sandbox-docker", "0.7.2", fetch=fetch, now=clock.now, sleep=clock.sleep
        )
        assert status == 0


class TestTheCli:
    def test_it_refuses_the_wrong_argument_count(self):
        assert await_live.main(["await_live_version.py", "maf-sandbox-docker"]) == 2
