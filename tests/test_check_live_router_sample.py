"""The match logic behind `scripts/check_live_router_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_router_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_router_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_HEALTHY = """\
== 1. Which backend serves is one argument ==

  selected='in-process'   -> router.backend.name == 'in-process'
  selected='docker'       -> router.backend.name == 'docker'
  selected omitted        -> router.backend.name == 'in-process'  (the first registered)

== 2. A spec may raise the bar. It may not change who serves ==

  spec asks for container isolation
    SandboxBackendNotPermitted: the 'operator' workload requires at least 'container'
    isolation, and sandbox backend 'in-process' declares 'none'.

  spec requires files_out
    SandboxCapabilityNotSupported: sandbox backend 'in-process' does not support files_out.

== 3. Disposal goes to every registered backend ==

  acquired on 'in-process' (not serving — a leftover from an earlier config)
  acquired on 'docker' (serving)
  it runs: 'routed'

  dispose_scope reached both backends and disposed 2 sandbox(es).

Completed 5 of 5 acts. Disposed 2 sandbox(es) across 2 backends.
Allowlisted host answered HTTP 200; an unlisted one answered HTTP 000.
"""


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_prose_may_be_reworded_without_moving_the_verdict(self):
        """Everything reworded here is explanation or a library refusal sentence, not API."""
        reworded = (
            _HEALTHY.replace("(the first registered)", "(whichever was listed first)")
            .replace("(not serving — a leftover from an earlier config)", "(idle)")
            .replace("reached both backends and disposed", "cleaned up")
        )
        assert check.assess(reworded) == []


class TestSelection:
    def test_a_selection_resolving_to_the_wrong_backend_is_caught(self):
        # `selected='docker'` answering 'in-process' would mean the name was ignored — the one
        # mechanism a host has for choosing.
        wrong = _HEALTHY.replace(
            "selected='docker'       -> router.backend.name == 'docker'",
            "selected='docker'       -> router.backend.name == 'in-process'",
        )
        reasons = check.assess(wrong)
        assert any("expected exactly 'docker'" in r for r in reasons), reasons

    def test_the_registration_order_default_is_pinned(self):
        # If this ever answered 'docker', either the sample changed its registration order or
        # the router stopped taking the first — and the second would be #328 landing.
        flipped = _HEALTHY.replace(
            "selected omitted        -> router.backend.name == 'in-process'",
            "selected omitted        -> router.backend.name == 'docker'",
        )
        assert any("first backend the sample registers" in r for r in check.assess(flipped))

    def test_a_missing_selection_line_is_caught(self):
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if "selected='docker'" not in line
        )
        assert any("act 1 did not run it" in r for r in check.assess(dropped))


class TestTheRefusals:
    def test_each_refusal_is_required_by_name(self):
        for exception in ("SandboxBackendNotPermitted", "SandboxCapabilityNotSupported"):
            reasons = check.assess(_HEALTHY.replace(exception, "SomethingElse"))
            assert any(exception in r for r in reasons), (exception, reasons)

    def test_a_reroute_would_read_as_a_missing_refusal(self):
        """What #328 landing would look like here, and why it must not pass silently.

        If the router began routing to the capable backend, these refusals would stop being
        printed and this check would go red — which is the correct outcome: the sample would be
        documenting behaviour that had changed underneath it.
        """
        routed = _HEALTHY.replace("SandboxCapabilityNotSupported", "served by 'docker' instead")
        assert any("rerouted or degraded" in r for r in check.assess(routed))


class TestTheWorkRanAndWasCleanedUp:
    def test_agreement_without_execution_is_caught(self):
        # `ensure_can_serve` returning proves the router agreed, not that anything ran.
        dropped = "\n".join(line for line in _HEALTHY.splitlines() if "it runs:" not in line)
        assert any("nothing proved one did" in r for r in check.assess(dropped))

    def test_a_near_miss_output_does_not_count_as_the_marker(self):
        """A container answering *wrongly* is not a container answering.

        Both of these contain `routed`, so a substring test read them as the marker — which
        would report a healthy run for a command that printed something else entirely.
        """
        for printed in ("unrouted", "routed with an error", "not routed"):
            near_miss = _HEALTHY.replace("it runs: 'routed'", f"it runs: '{printed}'")
            reasons = check.assess(near_miss)
            assert any("expected exactly 'routed'" in r for r in reasons), (printed, reasons)

    def test_an_unquoted_value_does_not_count(self):
        # The sample prints the value with `!r`. Anything else is not the line this parses.
        assert any(
            "expected exactly 'routed'" in r
            for r in check.assess(_HEALTHY.replace("it runs: 'routed'", "it runs: routed"))
        )

    def test_disposal_reaching_only_the_serving_backend_is_caught(self):
        """The assertion the whole sample exists for.

        Every other line stays correct when disposal misses the idle backend — the selection
        happened, the refusals fired, the command ran. Only this count moves.
        """
        leaked = _HEALTHY.replace(
            "Disposed 2 sandbox(es) across 2 backends", "Disposed 1 sandbox(es) across 2 backends"
        )
        reasons = check.assess(leaked)
        assert any("left a sandbox running" in r for r in reasons), reasons
        assert len(reasons) == 1, ("only the count should move", reasons)

    def test_a_second_backend_disappearing_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("across 2 backends", "across 1 backends"))
        assert any("a router holding more than one" in r for r in reasons), reasons

    def test_a_truncated_run_has_no_footer(self):
        cut = _HEALTHY.replace(
            "Completed 5 of 5 acts. Disposed 2 sandbox(es) across 2 backends.\n", ""
        )
        assert any("did not run to completion" in r for r in check.assess(cut))

    def test_a_partial_run_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("Completed 5 of 5", "Completed 2 of 5"))
        assert any("2 of 5 acts completed" in r for r in reasons), reasons

    def test_the_allowlist_act_skipping_itself_is_caught(self):
        """The one failure that reads exactly like a pass.

        Act 4 skips when no proxy image is named, and a skipped run still completes, still
        disposes two sandboxes and still prints every other line this file checks. Without the
        act count and the egress line it would be indistinguishable from a run that confined
        nothing, which is the whole reason the sample reports both.
        """
        skipped = _HEALTHY.replace("Completed 5 of 5", "Completed 4 of 5").replace(
            "Allowlisted host answered HTTP 200; an unlisted one answered HTTP 000.\n", ""
        )
        reasons = check.assess(skipped)
        assert any("4 of 5 acts completed" in r for r in reasons), reasons
        assert any("allowlist act did not run" in r for r in reasons), reasons


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []
