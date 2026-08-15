"""Pin per-package live-check concurrency so batch releases cannot silently drop checks (#325)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_LIVE = REPO_ROOT / ".github" / "workflows" / "verify-live.yml"
PUBLISH_PACKAGES = REPO_ROOT / ".github" / "workflows" / "publish-packages.yml"
_VERIFY_TEXT = VERIFY_LIVE.read_text("utf-8")
_PUBLISH_TEXT = PUBLISH_PACKAGES.read_text("utf-8")

#: The workflow-level group. Anchored at column zero so a job-level `concurrency:` — indented, and a
#: legitimate thing to add later — is not mistaken for this one.
_WORKFLOW_CONCURRENCY = re.compile(r"^concurrency:\n(?P<body>(?:[ \t]+\S.*\n)+)", re.MULTILINE)


def _workflow_concurrency_body() -> str:
    match = _WORKFLOW_CONCURRENCY.search(_VERIFY_TEXT)
    assert match is not None, "verify-live.yml has no workflow-level concurrency block"
    return match.group("body")


class TestTheGroupIsPerPackage:
    def test_the_group_interpolates_the_package_input(self):
        """A constant group is the regression: every call lands in it, and only two survive."""
        body = _workflow_concurrency_body()
        group = re.search(r"^\s*group:\s*(?P<value>.+?)\s*$", body, re.MULTILINE)
        assert group is not None, "the concurrency block declares no group"
        assert "inputs.package" in group.group("value"), (
            f"the live check's concurrency group does not vary by package: {group.group('value')!r}. "
            "A single group across every package loses verifications in a batch release — see #325."
        )

    def test_an_empty_package_still_names_its_group(self):
        # The verify-everything dispatch passes no package. Interpolating it bare would leave a
        # trailing dash, which works but reads as a truncation on the run page.
        body = _workflow_concurrency_body()
        assert re.search(r"inputs\.package\s*\|\|\s*'[^']+'", body), (
            "the group does not substitute a name for the empty (verify-everything) input"
        )

    def test_a_run_in_flight_is_never_cancelled(self):
        """The half of the original block that was always right, and is easy to lose in an edit.

        Cancelling mid-run leaves the billable sandbox to the backend's auto-delete timer rather
        than to the run that created it.
        """
        body = _workflow_concurrency_body()
        assert re.search(r"^\s*cancel-in-progress:\s*false\s*$", body, re.MULTILINE), (
            "verify-live.yml no longer sets cancel-in-progress: false"
        )


class TestTheCallerExplainsALostCheck:
    def test_a_cancelled_call_is_reported(self):
        """Displacement is rarer now, not impossible — two runs of one package still contend.

        The displaced run cannot report on itself, so the caller has to. Without this the loss is a
        cancelled called workflow and nothing else, which reads as a failed release.
        """
        assert re.search(r"needs\.verify\.result\s*==\s*'cancelled'", _PUBLISH_TEXT), (
            "publish-packages.yml no longer reports a superseded live check; a cancelled verify "
            "would read as a broken release (#325)"
        )

    def test_the_report_survives_the_cancellation(self):
        # A job whose only gate is `needs: verify` is skipped when verify is cancelled. `always()`
        # is what makes it run at all, so the two belong together.
        gate = re.search(
            r"^\s*if:\s*always\(\)\s*&&\s*needs\.verify\.result\s*==\s*'cancelled'\s*$",
            _PUBLISH_TEXT,
            re.MULTILINE,
        )
        assert gate is not None, (
            "the superseded-check report is not gated on always(), so it is skipped by the very "
            "cancellation it exists to explain"
        )
