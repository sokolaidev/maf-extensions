# Bounded OpenClaw actor-loss evidence

This cohort records the `owner-loss-windows-v1` profile for issue #1973. The directory date identifies the campaign cohort; execution crossed into 2026-10-20 in the operator's local time. It uses deterministic Gateway responses, an external HTTP caller and the installed published worker against real Docker resources.

## Accepted evidence

`candidate.json` binds runner commit `46457d9f`, the installed artifacts, dependencies, host, engine, images and configuration. Its canonical digest is `01c302cfa02f341cb149da075a86d28285cfd8e0a04e372103c5c3bad4a1eeb0`. `baseline/` retains the fresh eight-group installed-Bicep campaign `07275a0ad4df43aa8fa673cb12e35269`. `faults/` retains the separately planned fifteen-case fault campaign `48d6fda50c654d76acc81494eb733401`, including its baseline binding and all raw bounded observations. Preflight and postflight checks must match the same candidate. The evaluator reconstructs its required cases independently of the supplied report.

Re-evaluate the complete retained evidence without Docker, OpenClaw or credentials:

```powershell
$record = 'scripts/experiments/openclaw_owner_loss_evidence/2026-10-19-windows-v1'
uv run --no-project --python 3.13 python -m scripts.openclaw_conformance.owner_loss evaluate --bundle "$record/faults/evidence" --plan "$record/faults/plan.json" --manifest "$record/candidate.json" --baseline-bundle "$record/baseline/evidence" --baseline-plan "$record/baseline/plan.json"
```

The current evaluator additionally requires surviving-worker settlement to join the target tools/call exchange and runtime; the unchanged recorded campaign passes those predicates. This command evaluates recorded observations; it does not repeat live execution or qualify changed runner, dependency or platform bytes. Private installation inventories and configuration are not included. A new live run follows the preparation and qualification instructions in [the service document](../../../../docs/sandbox/openclaw.md#bounded-actor-loss-profile).

## Excluded development observations

`exploratory/inventory.json` identifies successful and failed collector probes by digest and explicitly excludes them from qualification. It also records setup failures that produced no case report. Those probes exposed a readiness timeout, a compiler barrier timeout, a cold Go build timeout and missing Docker image content in a dedicated-daemon setup. The exact pinned image content was restored before the final campaign; the image identity and finite profile limits were unchanged.

`superseded-baseline/` preserves the earlier baseline and its reported outcome without rewriting it. Its disposition excludes it from the fault qualification because qualification sources changed during that development execution. The accepted baseline above was run anew after the runner was committed. No stage from the superseded campaign supplies accepted evidence.

## Limits

Qualification covers only the selected candidate, identified actor processes, four explicit phase barriers and two scoped recovery-refusal controls. Worker recovery retains its owner and confirms cleanup before readiness; token rotation is an operator action. Independent harness cleanup and sentinel removal remain separate observations. Host power loss, arbitrary descendants, unattended watchdogs, other platforms, native MXC, credentials, real-model behavior and production acceptance are not established.
