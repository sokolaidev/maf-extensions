# Egress: a resolved mode, not a refused mismatch

> **Status: PROPOSED.** This is the target, written as one; where it and the code disagree, the code is what exists. It supersedes the egress half of [`sandbox-architecture.md`](sandbox-architecture.md)'s rule 3 (the egress-honesty refusal) and the `Egress` sketch in [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md). The isolation ladder it sits inside is [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md); the baseline it evolves is [`sandbox-architecture.md`](sandbox-architecture.md). What it closes across the issue tracker, and how it rolls out, is recorded on the umbrella issue rather than here.

---

## The problem the binary refusal has

Today egress is a single backend declaration (`Egress.ALLOWLIST | CLOSED | UNRESTRICTED | UNDEFINED`) and a single spec list (`egress_allow`), reconciled by one rule: **the router refuses a backend that would confine *less* than the spec's `egress_allow` asks.** The asymmetry is sound — confining less silently widens what a workload reaches; confining more only makes it fail loudly — and its spirit survives here. What does not survive contact with the suite is that the rule treats egress as a *property to match* rather than a *level to agree on*, and three real cases fall through the gap:

1. **A workload that names hosts but runs fine without them.** `bicep_sandbox_spec` names four hosts (`mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms`, `live-data.bicep.azure.com`) — where AVM modules restore from *if a template uses them*. [`samples/05_docker_bicep`](../../samples/05_docker_bicep) compiles a module-free template on `--network none` and completes fully offline; its README says so outright and calls the loud module-restore shortfall the point. So "names hosts" does **not** mean "requires the network," and any rule that equates them refuses a working sample.

2. **A backend that cannot confine at all has to lie to be used.** [`samples/09_inprocess_bicep`](../../samples/09_inprocess_bicep)'s no-isolation backend runs the bicep CLI on the host; it genuinely cannot confine egress. To pass the router it declares `Egress.CLOSED` — a claim it cannot enforce — and its own source comments call it a "temporary misuse." The honest declaration (`UNRESTRICTED`) is refused for every workload, so honesty is unusable.

3. **`Capability.NETWORK` is a rule keyed on nothing.** It is declared by no backend, required by no kind, read nowhere in the router (three greps, zero each). But the question it tries to answer — *does this workload need the network* — is not a yes/no a capability can hold, because bicep's answer is *it depends on the template*.

The common root: **"needs the network" is not a fixed property of a kind, and "confines the network" is not a single mode a backend either has or lacks.** Both are ranges, and the workload sits somewhere on the range per deployment. So resolve, don't match — and resolve to exactly what was asked or to nothing.

## The axis

One ordering, least-isolated to most-isolated:

```
UNRESTRICTED  <  ALLOWLIST  <  CLOSED
(reach anything)  (named hosts)  (no network)
```

`CLOSED` is the **most** isolated and the **default**: a spec that says nothing about egress gets no network, preserving the fail-closed property the `egress_allow` docstring already protects — *"a spec that forgets to mention egress gets the closed configuration, not the open one."*

## Three declarations

| who | declares | means | example |
| --- | --- | --- | --- |
| **workload** (the deployment building the spec) | `egress`: the **one mode it will run in**; default `CLOSED` | "run me in exactly this posture" | bicep validating a local template runs `CLOSED`; a deployment that permits AVM restores runs `ALLOWLIST` with the four hosts; the dev sample that accepts no confinement runs `UNRESTRICTED` |
| **kind** (the tool factory) | the set of modes it will **accept as an ask** — validated at construction, not carried on the spec | "these are the postures I can build myself for" | bicep `{UNRESTRICTED, ALLOWLIST, CLOSED}`; codeact `{CLOSED, ALLOWLIST}` — it computes offline or reaches allowed sources, and **never** runs unconfined, because model-written code reaching *anything* is the exfiltration case the allowlist exists to prevent; a render-only kind `{CLOSED}`; a kind that cannot function offline omits `CLOSED` |
| **backend** | `egress_modes`: the set of modes it can **enforce** | "I can actually deliver any of these — no more, no approximation" | docker-no-proxy `{CLOSED}`; docker-with-proxy `{CLOSED, ALLOWLIST}`; ACAS per group policy; in-process no-isolation `{UNRESTRICTED}` |

`egress_allow` (the hostnames) stays on the spec as the **payload of an `ALLOWLIST` run** — consulted only when the mode is `ALLOWLIST`, ignored otherwise. A non-empty `egress_allow` requires `egress == ALLOWLIST` (validated at spec construction), so the two cannot drift: naming hosts and running with no network is incoherent and is refused where it is written.

The kind's accepted-set is a **construction-time guard**, not a spec field: `bicep_sandbox_spec(egress=…)` refuses an argument outside its set, so a deployment cannot ask a render-only kind for `ALLOWLIST` or a must-fetch kind for `CLOSED`. The spec that reaches the router carries only the single resolved `egress` mode and, for `ALLOWLIST`, its host list. Backends declare a **set** rather than the single `Egress` value they carry today — that is the breaking change at the backend seam, and it is what lets `docker-with-proxy` say "I can enforce either `ALLOWLIST` or `CLOSED`." The old single `egress` property is removed. The set is exactly what the backend can *enforce*: a backend that cannot cut the network does not list `CLOSED`, however much it would like to.

## The resolution rule — refuse, never degrade

The workload's `egress` names the exact mode it will run in. Resolution is a **check, not a search**:

> Serve at `spec.egress` **iff the backend enforces it** (`spec.egress ∈ backend.egress_modes`). Otherwise **refuse at attach**.

> ### Refuse, never degrade.
> The router never substitutes a mode for the one asked. Not a **more open** one — that would silently widen what the workload reaches, the exact failure the axis exists to prevent. Not a **more isolated** one — that would hand the workload a quietly different network posture than it and its kind were built around, and call a half-run a success. A workload gets the egress it asked for, enforced, or it does not run. There is no best-effort: a backend is never handed a mode it cannot deliver.

The kind's accepted-set does not re-enter here — it was spent at construction, bounding what `spec.egress` could be. So resolution is one membership test, run in both `ensure_can_serve` (the host's wiring test) and `acquire` (so a caller who skips the wiring test is refused all the same).

**What happens to bicep's "runs offline, reports the shortfall"?** It moves from a resolution outcome to a runtime one, which is where it belongs. A deployment that permits no network builds bicep with `egress=CLOSED`; the run is served at `CLOSED`; if the *template* then references a module, the restore fails inside the sandbox and bicep reports it — a template/posture mismatch surfaced loudly at runtime, not a backend quietly confining less than asked. Sample 05 becomes exactly this: `egress=CLOSED`, served, and its module-free template completes with nothing to report.

## Worked table — bicep (`accepts {all three}`)

| backend (`egress_modes`) | run `CLOSED` (default) | run `ALLOWLIST` (4 hosts) | run `UNRESTRICTED` |
| --- | --- | --- | --- |
| **in-process** `{UNRESTRICTED}` | **refused** — cannot enforce `CLOSED`; not approximated | **refused** — cannot enforce `ALLOWLIST`; not widened to open | **UNRESTRICTED** — enforceable; the honest dev opt-in that **unblocks the in-process sample** |
| **docker-no-proxy** `{CLOSED}` | CLOSED | **refused** — cannot enforce `ALLOWLIST` | refused |
| **docker-with-proxy** `{CLOSED, ALLOWLIST}` | CLOSED | ALLOWLIST — restores from the four | refused |

The deployment picks the mode to match the backend it wired, and `ensure_can_serve` is the test that catches a mismatch at attach rather than at first call. Two readings worth stating:

- **The in-process column is the shape of the whole rule.** A backend that enforces nothing tighter than open serves **only** a workload that has explicitly asked to run open. `samples/09` stops declaring a `CLOSED` it cannot keep, declares `{UNRESTRICTED}`, and its bicep spec runs `UNRESTRICTED` — honest end to end: the backend claims only what it enforces, the workload states it accepts no confinement, and the router invents no middle ground.
- **The `ALLOWLIST`-on-`{CLOSED}` cell is a refusal, not a degrade.** A deployment that needs the four hosts and wires a backend that cannot allowlist is told so at attach, loudly, rather than served a `CLOSED` run whose failures it must reconstruct from a traceback. If that deployment does not actually need the hosts, it should run `CLOSED` and say so.

A **must-fetch kind** (its accepted-set omits `CLOSED`) is refused on any backend that enforces only `{CLOSED}`, for the same one reason every refusal here has: the asked mode is not in the backend's enforceable set. That is the footgun-guard — a kind that cannot function without the network is refused rather than silently run offline — falling out of the single rule rather than needing one of its own.

## Where this sits against the isolation ladder

Egress resolution decides *which mode the kind runs in*; it does not decide *how much to trust the boundary behind that mode* — that is the isolation floor's job, and it runs first. [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) caps a backend that cannot confine egress below `microvm`, and a host that requires real confinement sets `min_isolation` above `NONE`, which refuses the in-process backend before egress is ever resolved. So the in-process `{UNRESTRICTED}` backend is reachable only by a host that has already accepted a no-isolation, dev-machine posture — and even there, this design refuses to let it serve a workload that asked for isolation it cannot provide. The ladder gates *whether a boundary is real*; egress resolution, within that, gates *what the boundary is set to*, and never pretends one exists.

## What each layer changes

- **`SandboxSpec`** — `egress_allow: tuple[str, ...]` stays as the `ALLOWLIST` payload; a new `egress: Egress = Egress.CLOSED` carries the mode the workload runs in. Construction refuses a non-empty `egress_allow` unless `egress is ALLOWLIST`. No kind-set field: that guard lives in each kind's factory.
- **`SandboxBackend`** — `egress: Egress` (single) becomes `egress_modes: frozenset[Egress]`, the modes it can enforce. `getattr` silence is read as `frozenset()` — enforces nothing, so every ask is refused, which preserves today's "undeclared is refused" and makes the `UNDEFINED` value redundant at the ask path (it remains only as the honest name a backend may still give for "I considered this and enforce nothing").
- **The router** — rule 3's refusal is replaced by the one-line resolution above, in both `ensure_can_serve` and `acquire`. The resolved mode is handed to the backend at `acquire`, which enforces it and reads `egress_allow` when it is `ALLOWLIST`. `ensure_can_serve` reports the refusal reason so a host's wiring test sees why a pairing will not serve. `SandboxEgressNotEnforced` is kept and reworded: its old sentence ("cannot confine egress to what the spec allows") becomes "cannot enforce the `<mode>` egress this workload runs in (it enforces `<set>`)."
- **The kinds** — `bicep_sandbox_spec` and `codeact_sandbox_spec` gain an egress argument validated against their accepted-set, and build their internal logic from the mode. bicep accepts `{all three}` (a fixed compiler, low-risk unconfined, and the dev sample needs `UNRESTRICTED`); codeact accepts `{CLOSED, ALLOWLIST}` (model-written code, so never unconfined — closed to compute, allowlist to reach permitted sources).
- **`Capability.NETWORK`** — removed. The question it never answered (*does this workload need the network*) is now the per-deployment `egress` mode against the kind's accepted-set, which is where "it depends on the template" can actually live.
- **Conformance** — the capability suite gains egress-resolution probes: a workload running each mode against a backend enforcing each set, asserting served-exactly or refused, verified through `exec` since `Capability.FILES_IN` is in `DEFAULT_CAPABILITIES` and the backend that most needs these probes may serve no pull surface.
