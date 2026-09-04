"""The match logic behind `scripts/check_live_router_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.

Act 4 needs a chat model, and every other line here was produced without one, so that act's
block was rendered by the sample's own code paths against the same two images CI builds:
the same `bicep_validate` results, the same `evidence()` headings, the same counts. What is
**not** from a live run is the model's prose, which stands in as `_REPLY` — and that is the one
part no assertion below may depend on. `TestAModelCannotForgeAMeasurement` is where that is
made structural rather than hoped for.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_router_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_router_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: Where act 4 prints `quoted(reply.text)`. Deliberately bland: a test that only passes because
#: the model happened to be terse is a test of the model.
_REPLY = """\
The bicep_validate tool reported that the module restore failed, so the validation is
incomplete. The diagnostics it returned were BCP192 on line 25 and BCP062 on line 34.
"""

ACT_SIX_BLOCK = """\
  [measured] core supports per-spec selection: yes
  [measured] routed files_out spec -> 'docker'
  [measured] routed plain spec -> 'in-process'

  [measured] the routed backend runs: 'routed per spec'
  the routed sandbox is disposed, by the same scope purge act 5 measures
"""

_SKIPPED_ACT_SIX = """\
  [measured] core supports per-spec selection: no
  The published `maf-sandbox` this run resolved predates `Selection`.
"""

_HEALTHY = f"""\
== 1. Which backend serves is one argument ==

  [measured] selected='in-process'   -> router.backend.name == 'in-process'
  [measured] selected='docker'       -> router.backend.name == 'docker'
  [measured] selected omitted        -> router.backend.name == 'in-process'  (the first registered)

== 2. A spec may raise the bar. It may not change who serves ==

  spec asks for container isolation
  [measured] SandboxBackendNotPermitted: the 'operator' workload requires at least 'container'
  isolation, and sandbox backend 'in-process' declares 'none'.

  spec requires files_out
  [measured] SandboxCapabilityNotSupported: sandbox backend 'in-process' does not support
  files_out, which the 'operator' workload requires (it declares exec, files_in).

== 3. The other axis: what a backend can confine ==

  DockerSandboxConfig()                        -> closed
  DockerSandboxConfig(egress_proxy_image=...)   -> allowlist

== 4. An allowlist the workload wrote, and work that needs it ==

  main.bicep uses a br/public AVM module, so the compiler cannot type-check it
  without downloading one. `bicep_sandbox_spec()` names what that download needs:

    mcr.microsoft.com
    *.data.mcr.microsoft.com
    aka.ms
    live-data.bicep.azure.com

  --- egress closed ---

{_REPLY}
== bicep_validate under egress closed ==

  build(main.bicep): MODULE RESTORE FAILED for 1 module reference(s) (BCP190/BCP191/BCP192).
  build(main.bicep): 2 diagnostic(s)
    [error] BCP192 @ main.bicep:25: Unable to restore the artifact with reference
    "br:mcr.microsoft.com/bicep/avm/res/storage/storage-account:0.9.1" (Resource temporarily
    unavailable (mcr.microsoft.com:443))
    [error] BCP062 @ main.bicep:34: The referenced declaration with name "storage" is not valid.
  lint(main.bicep): MODULE RESTORE FAILED for 1 module reference(s) (BCP190/BCP191/BCP192).

  [measured] compiles that reached the sandbox: 1

  --- egress allowlist ---

{_REPLY}
== bicep_validate under egress allowlist ==

  build(main.bicep): no diagnostics
  lint(main.bicep): no diagnostics

  [measured] compiles that reached the sandbox: 1

  [measured] AVM restore under egress closed: FAILED
  [measured] AVM restore under egress allowlist: RESTORED (4 hosts allowed)

== 5. Disposal goes to every registered backend ==

  acquired on 'in-process' (not serving — a leftover from an earlier config)
  acquired on 'docker' (serving)
  [measured] it runs: 'routed'

  [measured] dispose_scope reached both backends and disposed 2 sandbox(es).

== 6. The spec picks, when the host asks it to ==

  [measured] core supports per-spec selection: yes
  [measured] routed files_out spec -> 'docker'
  [measured] routed plain spec -> 'in-process'

  [measured] the routed backend runs: 'routed per spec'
  the routed sandbox is disposed, by the same scope purge act 5 measures

  [measured] Completed 6 of 6 acts. Disposed 2 sandbox(es) across 2 backends.
"""

#: The same run against a published core that predates `Selection`. Act 6 says so and prints no
#: route, and the footer counts five — which is the one skip this check tolerates, and only
#: because the run states the reason rather than falling silent.
_OLDER_CORE = _HEALTHY.replace(ACT_SIX_BLOCK, _SKIPPED_ACT_SIX).replace(
    "Completed 6 of 6", "Completed 5 of 6"
)


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_prose_may_be_reworded_without_moving_the_verdict(self):
        """Everything reworded here is explanation, not a measurement."""
        reworded = (
            _HEALTHY.replace("(the first registered)", "(whichever was listed first)")
            .replace("(not serving — a leftover from an earlier config)", "(idle)")
            .replace("reached both backends and disposed", "cleaned up")
            .replace("(4 hosts allowed)", "(four hosts allowed)")
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
        happened, the refusals fired, the command ran, the module restored. Only this count
        moves.
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
            "  [measured] Completed 6 of 6 acts. Disposed 2 sandbox(es) across 2 backends.\n", ""
        )
        assert any("did not run to completion" in r for r in check.assess(cut))

    def test_a_partial_run_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("Completed 6 of 6", "Completed 2 of 6"))
        assert any("2 of 6 acts completed" in r for r in reasons), reasons


class TestTheEgressAct:
    """Act 4's pair, and why neither half is redundant."""

    def test_the_egress_act_skipping_itself_is_caught(self):
        """The one failure that reads exactly like a pass.

        Act 4 skips when any of its four variables is unset, and a skipped run still completes,
        still disposes two sandboxes and still prints every other line this file checks. Without
        the act count and the restore pair it would be indistinguishable from a run that
        confined nothing, which is the whole reason the sample reports both.
        """
        skipped = _HEALTHY.replace("Completed 6 of 6", "Completed 5 of 6")
        skipped = "\n".join(
            line for line in skipped.splitlines() if "AVM restore under egress" not in line
        )
        reasons = check.assess(skipped)
        assert any("5 of 6 acts completed" in r for r in reasons), reasons
        assert any("act 4 did not run that posture" in r for r in reasons), reasons

    def test_a_module_restoring_with_egress_closed_is_caught(self):
        """The finding that would matter most, and the one a passing run hides.

        A container that downloads a module while the backend declares `closed` has a route out
        the deployment never gave it. Every other line in the sample stays correct — and the
        allowlisted half still says RESTORED, so a check reading only the success would call
        this healthy.
        """
        leaky = _HEALTHY.replace(
            "AVM restore under egress closed: FAILED",
            "AVM restore under egress closed: RESTORED",
        )
        reasons = check.assess(leaky)
        assert any("had a route the deployment never gave it" in r for r in reasons), reasons
        assert len(reasons) == 1, ("only the closed verdict should move", reasons)

    def test_an_allowlist_that_does_not_serve_the_list_is_caught(self):
        """The other direction: the proxy ran and the four hosts still did not answer.

        Reported rather than tolerated, because a run where both postures fail proves only that
        the container had no network — which is what the closed half already said.
        """
        useless = _HEALTHY.replace(
            "AVM restore under egress allowlist: RESTORED",
            "AVM restore under egress allowlist: FAILED",
        )
        reasons = check.assess(useless)
        assert any("did not serve the list the workload asked for" in r for r in reasons), reasons

    def test_each_posture_is_required_on_its_own(self):
        for posture in ("closed", "allowlist"):
            dropped = "\n".join(
                line
                for line in _HEALTHY.splitlines()
                if f"AVM restore under egress {posture}:" not in line
            )
            reasons = check.assess(dropped)
            assert any(f"under egress {posture}' line" in r for r in reasons), (posture, reasons)


class TestAModelCannotForgeAMeasurement:
    """Act 4 puts a model's prose into the stream this file parses. It must not be readable.

    Sample 11 had no model until act 4 grew one, so every assertion above used to read a stream
    only the sample wrote. The `[measured]` tag is what restores that, and these are the cases
    that would have gone wrong without it.
    """

    def test_a_reply_impersonating_act_five_cannot_mask_a_real_failure(self):
        """The sharp one: act 4's reply is printed *before* act 5's marker.

        A checker that took the first line containing `it runs:` would read the model's, and a
        model claiming the container printed `routed` would then hide a container that did not.
        """
        forged = _HEALTHY.replace("it runs: 'routed'", "it runs: 'unrouted'").replace(
            _REPLY, _REPLY + "  it runs: 'routed'\n"
        )
        reasons = check.assess(forged)
        assert any("expected exactly 'routed'" in r for r in reasons), reasons

    def test_a_reply_carrying_the_tag_has_been_quoted_away(self):
        """`quoted()` prefixes `> ` to any model line starting with the tag.

        So the forgery arrives as a quotation, and a quotation is not a measurement.

        What makes this discriminating is the pair of them. Drop the `^\\s*` anchor from the
        pattern and the quoted line matches too — but it does not *win*, because it is printed
        before the real verdict and the later one would overwrite it. Only counting the matches
        turns that from luck into a rule, so this reads a healthy run and the next one reads the
        same forgery as two verdicts.
        """
        forged = _HEALTHY.replace(
            _REPLY,
            _REPLY + "> [measured] AVM restore under egress closed: RESTORED\n",
        )
        assert check.assess(forged) == []

    def test_a_second_verdict_is_refused_rather_than_resolved(self):
        """Two measured verdicts for one posture means the stream has another author.

        Reported instead of picked, in either direction: the first is where a model's reply
        would be and the last is where anything appended afterwards would be, so neither
        position is the measurement. This is also the guard that makes the test above bite.
        """
        doubled = _HEALTHY.replace(
            "  [measured] AVM restore under egress closed: FAILED",
            "  [measured] AVM restore under egress closed: RESTORED\n"
            "  [measured] AVM restore under egress closed: FAILED",
        )
        reasons = check.assess(doubled)
        assert any("appears 2 times" in r for r in reasons), reasons
        assert any("none of them can be trusted" in r for r in reasons), reasons

    def test_untagged_prose_answers_nothing(self):
        """Every needle this file looks for, written by the model, tagged by nobody."""
        prose = (
            "  selected='docker'       -> router.backend.name == 'in-process'\n"
            "  SandboxBackendNotPermitted was not raised\n"
            "  AVM restore under egress closed: RESTORED\n"
            "  Completed 6 of 6 acts. Disposed 9 sandbox(es) across 9 backends.\n"
        )
        assert check.assess(_HEALTHY.replace(_REPLY, _REPLY + prose)) == []

    def test_a_sample_that_stopped_tagging_goes_red(self):
        """The tag is a contract between two files, so dropping it must fail loudly.

        Silently accepting an untagged line would leave the check passing while the property it
        depends on — that the model cannot write these lines — had quietly gone away.
        """
        untagged = _HEALTHY.replace("  [measured] it runs:", "  it runs:")
        assert any("nothing proved one did" in r for r in check.assess(untagged))


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []


class TestTheRoutingAct:
    """Act 6, and the one skip this file tolerates.

    Two routes are required rather than one, and the second is the interesting one. A router
    that simply preferred the stronger backend would satisfy the `files_out` route and fail the
    `plain` one — and preferring the stronger backend is exactly the behaviour that would move
    a workload already running onto a billable one.
    """

    def test_an_older_core_skipping_the_act_passes(self):
        """The straddle case: the sample resolves the published wheel, which for a while will
        not have the feature. Saying so is what makes the absence readable."""
        assert check.assess(_OLDER_CORE) == []

    def test_a_run_that_says_nothing_about_support_is_caught(self):
        """Without the line, five-of-six is both a healthy older core and a broken current one,
        and nothing else in the output tells them apart."""
        silent = _HEALTHY.replace("  [measured] core supports per-spec selection: yes\n", "")
        reasons = check.assess(silent)
        assert any("core supports per-spec selection" in r for r in reasons), reasons

    def test_claiming_no_support_and_printing_a_route_is_refused(self):
        """Two claims about one run that contradict each other, so neither can be read —
        refused rather than resolved in favour of one, as a doubled restore verdict is."""
        contradictory = _HEALTHY.replace(
            "core supports per-spec selection: yes", "core supports per-spec selection: no"
        )
        reasons = check.assess(contradictory)
        assert any("not a measurement of this run" in r for r in reasons), reasons

    def test_the_refused_spec_not_reaching_the_second_backend_is_caught(self):
        """`files_out` is the spec act 2 is refused for. Anything but `docker` means the router
        never read past the backend that refuses it, which is the whole feature missing."""
        wrong = _HEALTHY.replace(
            "routed files_out spec -> 'docker'", "routed files_out spec -> 'in-process'"
        )
        reasons = check.assess(wrong)
        assert any("expected exactly 'docker'" in r for r in reasons), reasons

    def test_a_servable_spec_moving_off_the_first_registered_backend_is_caught(self):
        """The safety property, and the one a router that preferred the stronger backend would
        fail: routing may serve what is refused, never move what already runs."""
        moved = _HEALTHY.replace(
            "routed plain spec -> 'in-process'", "routed plain spec -> 'docker'"
        )
        reasons = check.assess(moved)
        assert any("relocate existing traffic" in r for r in reasons), reasons

    def test_each_route_is_required_on_its_own(self):
        for line in (
            "  [measured] routed files_out spec -> 'docker'\n",
            "  [measured] routed plain spec -> 'in-process'\n",
        ):
            reasons = check.assess(_HEALTHY.replace(line, ""))
            assert any("reported no route for it" in r for r in reasons), (line, reasons)

    def test_agreement_without_execution_is_caught(self):
        """The router naming a backend is not the same as the backend running anything — the
        distinction act 5 already draws, drawn again for the act that chose differently."""
        agreed = _HEALTHY.replace("  [measured] the routed backend runs: 'routed per spec'\n", "")
        reasons = check.assess(agreed)
        assert any("the routed backend runs" in r for r in reasons), reasons

    def test_act_five_s_marker_does_not_answer_for_act_six(self):
        """A run that re-read the earlier act's file would print `'routed'` here, and the two
        acts create different sandboxes — so the markers are different on purpose."""
        borrowed = _HEALTHY.replace(
            "the routed backend runs: 'routed per spec'", "the routed backend runs: 'routed'"
        )
        reasons = check.assess(borrowed)
        assert any("expected exactly 'routed per spec'" in r for r in reasons), reasons

    def test_an_older_core_that_also_lost_an_act_is_still_caught(self):
        """Five of six is only healthy when act 6 is the missing one."""
        reasons = check.assess(_OLDER_CORE.replace("Completed 5 of 6", "Completed 4 of 6"))
        assert any("4 of 6 acts completed" in r for r in reasons), reasons
