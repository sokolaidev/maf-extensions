"""The live configuration reaches `verify-live.yml` only if both halves of the wiring hold.

Its jobs read the `live-verify` environment for the Azure identity, the sandbox group and the
model endpoint. Two things have to be true for those to arrive: the job declares
`environment: live-verify`, and every caller passes `secrets: inherit`. The second is not
implied by the first — a called workflow resolves that environment's `vars` from the
`environment:` key alone, while its secrets arrive empty (actions/runner#4453).

Neither half fails loudly. A missing `secrets: inherit` leaves the values empty rather than
absent, so the failure lands after a package is already on PyPI, in a job that reads as an
Azure problem. These pin the shape instead.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
VERIFY_LIVE = WORKFLOWS / "verify-live.yml"


def _jobs(path: Path) -> dict[str, dict]:
    workflow = yaml.safe_load(path.read_text("utf-8")) or {}
    return {
        name: definition
        for name, definition in (workflow.get("jobs") or {}).items()
        if isinstance(definition, dict)
    }


def _callers() -> list[tuple[str, str, dict]]:
    """Every job, in any workflow, that calls `verify-live.yml`."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, definition in _jobs(path).items():
            uses = str(definition.get("uses", ""))
            if uses.split("@")[0].endswith("verify-live.yml"):
                found.append((path.name, name, definition))
    return found


class TestEveryCallerInheritsSecrets:
    def test_the_workflow_is_called_from_somewhere(self):
        # Without this the test below passes vacuously once nothing calls it.
        assert _callers(), "no workflow calls verify-live.yml"

    def test_each_call_inherits(self):
        for workflow, job, definition in _callers():
            assert definition.get("secrets") == "inherit", (
                f"{workflow}:{job} calls verify-live.yml without `secrets: inherit`, "
                f"so its environment secrets resolve empty"
            )


class TestEveryJobReadingASecretDeclaresTheEnvironment:
    @staticmethod
    def _reading_secrets() -> dict[str, dict]:
        return {
            name: definition
            for name, definition in _jobs(VERIFY_LIVE).items()
            if "secrets." in yaml.safe_dump(definition)
        }

    def test_the_workflow_still_reads_secrets(self):
        # Without this the test below passes vacuously on a file that stopped reading any.
        assert len(self._reading_secrets()) >= 7

    def test_each_one_names_live_verify(self):
        for job, definition in self._reading_secrets().items():
            assert definition.get("environment") == "live-verify", (
                f"{job} reads a secret without declaring the environment that holds it"
            )
