"""The match logic behind `scripts/check_live_host_tools_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_host_tools_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_host_tools_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# A real run of the sample, trimmed to the lines the check reads. Kept in the shape the sample
# prints — leading spaces and all — because that shape is what `_line_containing` scans.
_HEALTHY = """\
== 1. Registration ==

  registered: fetch_changelog, publish_release_note, semver_bump
  refused:    rerun_failed_jobs — host tool 'rerun_failed_jobs' has no complete
              information-flow declaration and this registry requires one.

== 2. What the surface means ==

  result_integrity:  untrusted
  outbound_caps:     {'public'}
  identities:        {app, user}
  requires_approval: True
  has_undeclared:    False  (the gate refused the fourth)

  sealed:     host tool 'semver_bump_again' cannot be registered: this registry was sealed.

== 3. A host that permits it ==

  ensure_can_serve('release-notes') returned. The kind may attach.

== 4. The two refusals ==

  denied_capabilities={HOST_TOOLS}
    SandboxCapabilityDenied: the 'release-notes' workload requires host_tools, which this
    host's router denies outright (denied_capabilities).

  denied_identities={USER}
    SandboxIdentityDenied: the 'release-notes' workload's dispatched tools exercise user
    authority, which this host's router denies outright (denied_identities).

  The way past the second refusal is a different registry, not a different call:
    a registry without publish_release_note folds to identities={app}, requires_approval=False,
    and the same denied_identities router serves the spec built from it.

Completed 4 of 4 acts. Acquired 0 sandbox(es).
"""


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_prose_may_be_reworded_without_moving_the_verdict(self):
        """The risk an exact-matching check carries, and the one it must not have.

        Everything reworded here is explanation or a library refusal sentence. Neither is API,
        and a check that went red on either would make wording changes cost a release cycle.
        """
        reworded = (
            _HEALTHY.replace("The kind may attach.", "So the kind is allowed to attach.")
            .replace("(the gate refused the fourth)", "(as the gate refused one)")
            .replace("which this host's router denies outright", "refused by this host")
            .replace("this registry was sealed.", "the surface is sealed and will not widen.")
        )
        assert check.assess(reworded) == []


class TestTheRegistrationGate:
    def test_a_stamped_tool_missing_from_the_surface_is_named(self):
        reasons = check.assess(_HEALTHY.replace("fetch_changelog, ", ""))
        assert any("fetch_changelog" in r for r in reasons), reasons

    def test_the_unstamped_tool_registering_is_caught(self):
        """The half that would go unnoticed: the refusal prints *and* the tool registers.

        Asserting only that a refusal line exists would pass this, because the sample prints
        both lines and only one of them changed.
        """
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
        # If the weakest-tier fold stopped working, a source-carrying registry would report
        # `trusted` — the exact silent widening the fold exists to prevent.
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
        # A run where every refusal fired but nothing was ever served would mean the router
        # refuses regardless, which passes the two tests above and proves nothing.
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if "ensure_can_serve" not in line
        )
        assert any("permitted path" in r for r in check.assess(dropped))


class TestWideningIsCaught:
    """Equal, not contains — the distinction the whole "strict check" claim rests on.

    A membership test sees a value disappear and cannot see one appear, so every case here
    passed before the parsing was made exact. Widening a published surface is the direction
    that matters: a tool nobody classified, an identity nobody denied, a confidentiality cap
    nobody reconciled.
    """

    def test_an_extra_identity_does_not_pass_on_the_two_it_kept(self):
        reasons = check.assess(_HEALTHY.replace("{app, user}", "{app, service, user}"))
        assert any("expected exactly ['app', 'user']" in r for r in reasons), reasons

    def test_a_second_outbound_cap_is_caught(self):
        # The diagnostic always claimed caps "must arrive exactly as declared"; the code did
        # not enforce it, so the message and the check disagreed.
        reasons = check.assess(_HEALTHY.replace("{'public'}", "{'public', 'private'}"))
        assert any("expected exactly ['public']" in r for r in reasons), reasons

    def test_an_extra_registered_tool_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace("registered: fetch_changelog", "registered: sneaky, fetch_changelog")
        )
        assert any("unexpected ['sneaky']" in r for r in reasons), reasons

    def test_a_renamed_tool_does_not_satisfy_the_original(self):
        # `fetch_changelog_backup` contains `fetch_changelog`, so a substring test read the
        # renamed tool as the one it replaced.
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
        # `has_undeclared: False  (the gate refused the fourth)` — exact on the token, not on
        # the rest of the line, or the sample could not annotate its own output.
        assert check.assess(_HEALTHY) == []
        annotated = _HEALTHY.replace(
            "has_undeclared:    False  (the gate refused the fourth)",
            "has_undeclared:    False  (because the gate turned the fourth away)",
        )
        assert check.assess(annotated) == []


class TestTheNarrowedRegistry:
    """Act 4's way out: a smaller surface registered from the start, run rather than described.

    The claim behind "least privilege is what a host registers". It cannot be shown by editing
    what act 3 built — `SandboxSpec` is frozen, `aggregate()` sealed the registry, and there is
    no unregister — so the sample builds a second registry, and these pin what it comes to.
    """

    def test_the_narrowed_fold_is_reported(self):
        assert check.assess(_HEALTHY) == []

    def test_a_missing_narrowing_step_is_caught(self):
        dropped = "\n".join(
            line for line in _HEALTHY.splitlines() if "folds to identities=" not in line
        )
        assert any("narrowed registry" in r for r in check.assess(dropped))

    def test_approval_surviving_the_narrowing_is_caught(self):
        # Dropping the only USER tool must take the whole surface off approval-gated. If it did
        # not, the identity refusal would still apply and the sample's way out would not work.
        reasons = check.assess(
            _HEALTHY.replace("requires_approval=False", "requires_approval=True")
        )
        assert any("expected exactly 'False'" in r for r in reasons), reasons

    def test_the_comma_the_sample_prints_is_not_read_as_the_value(self):
        # `requires_approval=False,` sits mid-sentence, so the token carries the clause's comma.
        # A verdict that turned on punctuation would be red on every healthy run.
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
        """The inverse of every other live check, and the sample's whole claim.

        Elsewhere `Disposed 0` is the failure. Here acquiring even one means a decision this
        sample says is answered at attach was answered somewhere later.
        """
        reasons = check.assess(_HEALTHY.replace("Acquired 0 sandbox", "Acquired 1 sandbox"))
        assert any("were acquired" in r for r in reasons), reasons


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []
