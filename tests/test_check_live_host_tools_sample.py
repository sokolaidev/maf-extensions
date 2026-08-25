"""The match logic behind `scripts/check_live_host_tools_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, verbatim but for the three lines the environment
contributes. Trimming it by hand is how it came to be missing the line #572 added, so
`TestTheFixtureIsStillWhatTheSamplePrints` runs the sample rather than trusting the fixture.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_host_tools_sample.py"
_SAMPLE = _ROOT / "samples" / "10_inprocess_host_tools" / "agent.py"
_spec = importlib.util.spec_from_file_location("check_live_host_tools_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# A real run, verbatim: the shape the sample prints is what `_line_containing` scans.
_HEALTHY = """\
== 1. Registration ==

  refused:    publish_release_note (registry is APP-only) — host tool 'publish_release_note' exercises 'user' authority, which this registry does not allow (allowed_identities: app). A host that means to run tools under this authority opts in at construction with allowed_identities=frozenset({Identity.APP, Identity.USER}); a tool declaring identity=None exercises no authority and is always allowed. denied_identities on the router stays the attach-time backstop.

  notice: a host tool was registered for calling from a sandbox: host-tool calls run in the host process with the host's authority and bypass the middleware chain — the boundary sees only execute_code's aggregate result. Suppress this notice with warnings.filterwarnings('ignore', category=MafSandboxHostToolsWarning) once read.

  registered: fetch_changelog, publish_release_note, semver_bump
  refused:    rerun_failed_jobs — host tool 'rerun_failed_jobs' has no complete information-flow declaration and this registry requires one. Stamp it with @sandbox_tool(source=..., sink=..., identity=...) — every leg answered; None is an answer, an omission is not.

  the callable surface is 3 functions, and nothing else.
  Least privilege here is what was registered, not what was declared.

== 2. What the surface means ==

  result_integrity:  untrusted
                     the weakest tier over sources only — fetch_changelog drags
                     the whole result down, and semver_bump cannot drag it up.
  outbound_caps:     {'public'}
                     verbatim and unfolded: confidentiality is the host's
                     vocabulary, and this library never guesses at an ordering.
  identities:        {app, user}
  requires_approval: True
                     because one USER tool is enough — a single host-tool call
                     could exercise the user's delegated authority.
  has_undeclared:    False  (the gate refused the fourth)

  sealed:     host tool 'semver_bump_again' cannot be registered: this registry was sealed when its aggregate was taken, and a host has already derived a spec and a classification from the surface as it stood. Widening it now would let a guest call what nothing classified.

== 3. A host that permits it ==

  ensure_can_serve('release-notes') returned. The kind may attach.
  One call is the whole of a host's wiring test — and the whole of this sample's
  happy path, because a host-tool call needs a guest program and this sample runs none.

== 4. The two refusals ==

  denied_capabilities={HOST_TOOLS}
    SandboxCapabilityDenied: the 'release-notes' workload requires host_tools, which this host's router denies outright (denied_capabilities). A hard stop rather than a missing feature: whatever backend is registered, this posture refuses the capability — serve the workload on a host that permits it, or narrow what it requires.

  denied_identities={USER}
    SandboxIdentityDenied: the 'release-notes' workload's host tools exercise user authority, which this host's router denies outright (denied_identities). Remove the tools declaring that identity from the workload's registry, or serve it on a host whose posture permits them.

  Both are PermissionError, both name the deployment's own setting, and both
  turn away the whole kind rather than one function — there is no partial
  attach.

  The way past the second refusal is a different registry, not a different call:
    a registry without publish_release_note folds to identities={app}, requires_approval=False,
    and the same denied_identities router serves the spec built from it.
    Least privilege is what a host registers, and the cost of that is real:
    the spec is frozen, the registry sealed, and there is no unregister.

== What is not here ==

  A host-tool call. The transport a guest sends a request over has landed (#327),
  maf-sandbox-codeact makes host-tool calls over it, and the docker and acas backends
  declare Capability.HOST_TOOLS — so one would run. It needs a real sandbox,
  a guest program and a model, and this sample uses none of the three (#302).
  Everything above is the half a host configures on day one regardless, and it
  is the half that decides whether the other half ever runs.

Completed 4 of 4 acts. Acquired 0 sandbox(es).
"""


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_prose_may_be_reworded_without_moving_the_verdict(self):
        """Prose is not API, so rewording it must not move the verdict."""
        reworded = (
            _HEALTHY.replace("The kind may attach.", "So the kind is allowed to attach.")
            .replace("(the gate refused the fourth)", "(as the gate refused one)")
            .replace("which this host's router denies outright", "refused by this host")
            .replace("this registry was sealed when", "the surface will not widen once")
        )
        # A replacement matching nothing would leave the fixture untouched.
        assert reworded != _HEALTHY
        for gone in (
            "The kind may attach.",
            "(the gate refused the fourth)",
            "which this host's router denies outright",
            "this registry was sealed when",
        ):
            assert gone not in reworded, gone
        assert check.assess(reworded) == []


class TestTheRegistrationGate:
    def test_a_stamped_tool_missing_from_the_surface_is_named(self):
        reasons = check.assess(_HEALTHY.replace("fetch_changelog, ", ""))
        assert any("fetch_changelog" in r for r in reasons), reasons

    def test_the_unstamped_tool_registering_is_caught(self):
        """The refusal printing *and* the tool registering — what a refusal check alone misses."""
        leaked = _HEALTHY.replace(
            "  registered: fetch_changelog",
            "  registered: rerun_failed_jobs, fetch_changelog",
        )
        assert any("unexpected ['rerun_failed_jobs']" in r for r in check.assess(leaked)), (
            check.assess(leaked)
        )

    def test_the_gate_not_firing_at_all_is_caught(self):
        dropped = "\n".join(line for line in _HEALTHY.splitlines() if "refused:" not in line)
        assert any("require_declared gate did not fire" in r for r in check.assess(dropped))


class TestTheAggregate:
    def test_a_changed_fold_is_caught_by_leg(self):
        # `trusted` is the silent widening the weakest-tier fold exists to prevent.
        reasons = check.assess(
            _HEALTHY.replace("result_integrity:  untrusted", "result_integrity:  trusted")
        )
        assert any("result_integrity" in r for r in reasons), reasons

    def test_approval_dropping_is_caught(self):
        # One USER tool must raise the whole surface. False here would mean it stopped.
        reasons = check.assess(
            _HEALTHY.replace("requires_approval: True", "requires_approval: False")
        )
        assert any("requires_approval" in r for r in reasons), reasons

    def test_an_undeclared_tool_slipping_into_the_fold_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace("has_undeclared:    False", "has_undeclared:    True")
        )
        assert any("has_undeclared" in r for r in reasons), reasons

    def test_a_dropped_sink_cap_is_caught(self):
        # Carried verbatim and unfolded, so it must arrive exactly as declared.
        reasons = check.assess(_HEALTHY.replace("{'public'}", "set()"))
        assert any("outbound_caps" in r for r in reasons), reasons

    def test_a_missing_identity_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("{app, user}", "{app}"))
        assert any("identities" in r for r in reasons), reasons

    def test_the_seal_not_firing_is_caught(self):
        dropped = "\n".join(line for line in _HEALTHY.splitlines() if "sealed:" not in line)
        assert any("sealed" in r for r in check.assess(dropped))


class TestTheTwoRefusals:
    def test_each_deny_axis_is_required_by_name(self):
        for exception in ("SandboxCapabilityDenied", "SandboxIdentityDenied"):
            reasons = check.assess(_HEALTHY.replace(exception, "SomethingElse"))
            assert any(exception in r for r in reasons), (exception, reasons)

    def test_the_permitted_path_is_required_too(self):
        # A router refusing everything would pass the two tests above and prove nothing.
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if "ensure_can_serve" not in line
        )
        assert any("permitted path" in r for r in check.assess(dropped))


class TestWideningIsCaught:
    """Equal, not contains: a membership test cannot see a published surface widen."""

    def test_an_extra_identity_does_not_pass_on_the_two_it_kept(self):
        reasons = check.assess(_HEALTHY.replace("{app, user}", "{app, service, user}"))
        assert any("expected exactly ['app', 'user']" in r for r in reasons), reasons

    def test_a_second_outbound_cap_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("{'public'}", "{'public', 'private'}"))
        assert any("expected exactly ['public']" in r for r in reasons), reasons

    def test_an_extra_registered_tool_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace("registered: fetch_changelog", "registered: sneaky, fetch_changelog")
        )
        assert any("unexpected ['sneaky']" in r for r in reasons), reasons

    def test_a_renamed_tool_does_not_satisfy_the_original(self):
        # `fetch_changelog_backup` contains `fetch_changelog`, which a substring test accepted.
        reasons = check.assess(_HEALTHY.replace("fetch_changelog,", "fetch_changelog_backup,"))
        assert any("missing ['fetch_changelog']" in r for r in reasons), reasons

    def test_a_scalar_leg_is_compared_as_a_token(self):
        for original, tampered in (
            ("result_integrity:  untrusted", "result_integrity:  not-untrusted"),
            ("requires_approval: True", "requires_approval: True-ish"),
        ):
            reasons = check.assess(_HEALTHY.replace(original, tampered))
            assert any("expected exactly" in r for r in reasons), (tampered, reasons)

    def test_the_explanatory_suffix_is_still_allowed(self):
        # Exact on the token, not the rest of the line, or the sample could not annotate itself.
        assert check.assess(_HEALTHY) == []
        annotated = _HEALTHY.replace(
            "has_undeclared:    False  (the gate refused the fourth)",
            "has_undeclared:    False  (because the gate turned the fourth away)",
        )
        assert check.assess(annotated) == []


class TestTheNarrowedRegistry:
    """Act 4's way out: a smaller surface registered from the start, run rather than described."""

    def test_the_narrowed_fold_is_reported(self):
        assert check.assess(_HEALTHY) == []

    def test_a_missing_narrowing_step_is_caught(self):
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if "folds to identities=" not in line
        )
        assert any("narrowed registry" in r for r in check.assess(dropped))

    def test_approval_surviving_the_narrowing_is_caught(self):
        # Dropping the only USER tool must take the whole surface off approval-gated.
        reasons = check.assess(
            _HEALTHY.replace("requires_approval=False", "requires_approval=True")
        )
        assert any("expected exactly 'False'" in r for r in reasons), reasons

    def test_the_comma_the_sample_prints_is_not_read_as_the_value(self):
        # `requires_approval=False,` sits mid-sentence, so the token carries the comma.
        assert check.assess(_HEALTHY) == []

    def test_user_surviving_the_narrowing_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("identities={app}", "identities={app, user}"))
        assert any("expected exactly ['app']" in r for r in reasons), reasons


class TestTheRunCompleted:
    def test_a_truncated_run_has_no_completion_line(self):
        cut = _HEALTHY.replace("Completed 4 of 4 acts. Acquired 0 sandbox(es).\n", "")
        assert any("did not run to completion" in r for r in check.assess(cut))

    def test_a_partial_run_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("Completed 4 of 4", "Completed 2 of 4"))
        assert any("2 of 4 acts completed" in r for r in reasons), reasons

    def test_acquiring_a_sandbox_is_a_failure_here(self):
        """The inverse of every other live check: acquiring even one sandbox is the failure."""
        reasons = check.assess(_HEALTHY.replace("Acquired 0 sandbox", "Acquired 1 sandbox"))
        assert any("were acquired" in r for r in reasons), reasons


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []


class TestMoreThanOneRefusalIsPrinted:
    """The sample prints two `refused:` lines, and #572's APP-only one comes first."""

    def test_the_earlier_refusal_really_does_come_first(self):
        """Without this ordering the two below would pass for the wrong reason."""
        refusals = [line for line in _HEALTHY.splitlines() if "refused:" in line]
        assert len(refusals) == 2, refusals
        assert "registry is APP-only" in refusals[0]
        assert "rerun_failed_jobs" in refusals[1]

    def test_the_gate_is_still_found_behind_it(self):
        assert check.assess(_HEALTHY) == []

    def test_dropping_only_the_gate_line_is_still_caught(self):
        """The APP-only line stays, so a check happy with any refusal would pass this."""
        dropped = "\n".join(
            line
            for line in _HEALTHY.splitlines()
            if not ("refused:" in line and "rerun_failed_jobs" in line)
        )
        assert any("registry is APP-only" in line for line in dropped.splitlines())
        assert any("require_declared gate did not fire" in r for r in check.assess(dropped))


class TestAKeyIsNotTheTailOfALongerKey:
    """`allowed_identities:` ends in `identities:`, and the refusal prints it first."""

    def test_the_trap_line_is_in_the_output_and_comes_first(self):
        lines = _HEALTHY.splitlines()
        trap = next(i for i, line in enumerate(lines) if "allowed_identities:" in line)
        real = next(i for i, line in enumerate(lines) if line.strip().startswith("identities:"))
        assert trap < real, (trap, real)

    def test_the_aggregate_line_is_the_one_read(self):
        assert any("identities" in r for r in check.assess("")), "empty output must still fail"
        assert check.assess(_HEALTHY) == []

    def test_the_trap_line_cannot_stand_in_for_a_missing_one(self):
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if not line.strip().startswith("identities:")
        )
        assert any("allowed_identities:" in line for line in dropped.splitlines())
        assert any("identities were not reported" in r for r in check.assess(dropped))


class TestTheFixtureIsStillWhatTheSamplePrints:
    """Runs the sample, because every other assertion here reads the fixture."""

    def test_a_real_run_passes_the_check(self):
        completed = subprocess.run(
            [sys.executable, str(_SAMPLE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert completed.returncode == 0, completed.stderr
        assert check.assess(completed.stdout) == [], completed.stdout

    def test_every_line_the_check_reads_is_in_the_fixture(self):
        """Says which line drifted, where the test above only says the check failed."""
        completed = subprocess.run(
            [sys.executable, str(_SAMPLE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert completed.returncode == 0, completed.stderr
        keys = ("refused:", "registered:", "identities:", "sealed:", "ensure_can_serve")
        for key in keys:
            live = [line.strip() for line in completed.stdout.splitlines() if key in line]
            fixture = [line.strip() for line in _HEALTHY.splitlines() if key in line]
            assert live == fixture, (key, live, fixture)

    def test_the_fixture_is_verbatim_but_for_the_environment_line(self):
        """Every line, not just the ones `assess` reads.

        The keys above cover what the checker scans, so a line it ignores could drift from
        anything the sample can print — which is how a rename once reached the `notice:` and
        `SandboxIdentityDenied:` lines here while both production strings still said dispatch.
        """
        completed = subprocess.run(
            [sys.executable, str(_SAMPLE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert completed.returncode == 0, completed.stderr
        live = [
            line
            for line in completed.stdout.splitlines()
            if not line.strip().startswith("[measured] installed:")
        ]
        assert live == _HEALTHY.splitlines()


class TestALabelIsNotProseOnAnotherLine:
    """`cannot be registered:` sits mid-sentence on the sealed line, one lookup away."""

    def test_the_sealed_line_holds_the_registered_key(self):
        """The collision is real; only line order kept it from being read."""
        holds = [line for line in _HEALTHY.splitlines() if "registered:" in line]
        assert len(holds) == 2, holds
        assert holds[0].strip().startswith("registered:")
        assert "cannot be registered:" in holds[1]

    def test_the_sealed_line_cannot_stand_in_for_a_missing_surface(self):
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if not line.strip().startswith("registered:")
        )
        assert any("cannot be registered:" in line for line in dropped.splitlines())
        assert any("act 1 did not run" in r for r in check.assess(dropped))

    def test_a_label_printed_twice_is_refused(self):
        """Two answers, and taking the first would be choosing which to believe."""
        doubled = _HEALTHY.replace(
            "  identities:        {app, user}",
            "  identities:        {app, user}\n  identities:        {app}",
        )
        assert any("labels 2 lines" in r for r in check.assess(doubled)), check.assess(doubled)

    def test_every_label_appears_once_in_a_healthy_run(self):
        assert check._assess_each_label_appears_once(_HEALTHY) == []
