# Backends

> What each shipped backend declares to the router, what it does with a sandbox's life, and where it is honestly quirky.

## The boundary these pages keep

A backend has two audiences and they want different documents. These pages own the **architecture-facing contract**: the four declarations the router reads, lifecycle behaviour, conformance status, and the quirks a kind author is entitled to know before choosing where a workload runs. Each package's own README owns **installation, configuration fields and usage**, and is linked from its page here rather than copied into it — a sentence that lives in both places is a sentence that will drift, and the copy nobody edits is the one somebody reads.

## The four declarations

Every backend answers four questions before a workload's tool is ever attached, and the router acts on each one. **`isolation`** is a rung on an ordered ladder, checked against the stricter of the host's floor and the spec's; below it, construction raises rather than degrading. **`capabilities`** is a frozenset matched against the spec's `requires`, and silence is read charitably as `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what `Sandbox` already obligates. **`egress`** states what the backend *can confine*, not what any one sandbox got, and silence is read as `UNDEFINED` and refused, because a backend written before the property existed cannot have been enforcing an allowlist it never read. **`limits`** declares transfer ceilings per direction and follows the egress rule rather than the capability one — a cap is a safety claim, so silence resolves to `DEFAULT_SANDBOX_LIMITS`, never to "no ceiling". The checks themselves, and who owns each, are in [`../policy-isolation.md`](../policy-isolation.md).

## The shipped backends

| | [`acas`](acas.md) | [`docker`](docker.md) | [`wslc`](wslc.md) | [`in-process`](in-process.md) |
|---|---|---|---|---|
| **Isolation** | `microvm` — a hardware-isolated micro-VM, a property of the Azure service rather than of this code; meets the router's default floor with nothing configured | `container` — a constant no configuration raises; a host opts the floor down explicitly | `container` — the same opt-down, for the same reason | `none` — and it runs nothing in a boundary at all |
| **Capabilities** | `EXEC, FILES_IN, FILES_OUT, FILES_LIST, HOST_TOOLS` — the only backend that can enumerate, which is the capability split's own test applied to itself | `EXEC, FILES_IN, FILES_OUT, FILES_DELETE, HOST_TOOLS` — never `FILES_LIST` | `EXEC, FILES_IN` | `DEFAULT_CAPABILITIES`, though the sandbox implements more than it declares |
| **Egress** | `allowlist`: `default_action: Deny` plus one `Allow` per host, built **from the spec** | `closed` by default; `allowlist` when an egress proxy image is configured | the same two modes, by the same topology | `allowlist` |
| **Limits** | declared — 32 MiB per file, 128 MiB total, 128 files, each direction | declared — 64 MiB per file, 256 MiB total, 256 files, each direction | **not declared** — silence resolves to `DEFAULT_SANDBOX_LIMITS` | `DEFAULT_SANDBOX_LIMITS` |
| **Identity** | the control-plane credential never travels into the guest; no path from inside back to the host's identity or another conversation's sandbox | none attached | none attached | the host's own process, with the host's authority — which is what `none` says |
| **Reuse & purge** | warm resume over cold create; labels at create, purge by label from the service; get-or-create serialised per key | reuse, restart, adopt-on-name-conflict; labels at create, purge by label from the engine | sub-second creates; egress scaffolding re-ensured on every acquire; labels and label purge | records every key, spec, dispose and purge; `acquire_error` for a kind's degrade path |
| **Where it runs** | anywhere with the service reachable | macOS, Linux, Windows with WSL 2, and CI runners | Windows with WSL only | this process |

Every cell above is read from each backend's `_backend.py`, which is the source of record; the router's own view of the same rows is [`../policy-isolation.md`](../policy-isolation.md) § "The map", and what each capability obligates is [`../capabilities.md`](../capabilities.md).

## What every backend is held to

**Declare honestly or not at all.** There is no router-side emulation of a capability a backend lacks, and there must not be: the whole value of the match is that a refusal at attach means the workload would genuinely not have run. A mechanism can exist and stay undeclared — ACAS implements `remove` and withholds `FILES_DELETE` — and that asymmetry is the safe direction.

**Purge consults the service, never process memory.** A conversation delete lands on whichever replica serves it, which is usually not the replica that created the sandbox, so a backend that sweeps its own dictionary leaks billable compute on every multi-replica host. Labels are written **at create**, so a sandbox is reachable by the identity a later purge will select on even if everything after the create fails. Label values are **hashed rather than truncated** when they will not pass through intact: two scopes sharing a prefix would land on the same label, and one conversation's purge would then delete another's sandboxes. The mapping has to be identical on write and on query, or the purge quietly selects nothing — which looks exactly like a clean tenant.

**The acquire race is real and invisible on one machine.** The function calls in a single assistant message execute concurrently, so two acquires for one key can be in flight at once. A backend either serialises its get-or-create or derives a name the provider rejects duplicates of; an unguarded read-then-create hands out two sandboxes and remembers one. The shipped backends do both where they can — a per-key lock, plus an adopt-on-name-conflict path for the race that lives in another process and no lock can see.

**base64-over-exec is opt-in convenience, never the contract.** It depends on `base64` and a shell existing in the image, which the native copy paths do not. If it ever ships it is one reviewed implementation in `maf_sandbox`, never a parse per backend, and it carries its own lower maxima.

**A backend claiming `microvm` *and* `FILES_OUT` owes an extra clause.** The declared channel is reads confined to the working directory with non-regular entries refused — the micro-VM standard's fourth leg applied to the out-door. See [`../policy-isolation.md`](../policy-isolation.md) § "The micro-VM standard".

**`maf_sandbox.testing` grows every protocol surface, or no kind can be tested.** The in-process fake is not an optional convenience: a kind is written against the protocol and nothing else, so a protocol member the fake does not implement is a member no kind's test suite can exercise offline. It implements stat, read, list and remove today and declares capabilities configurably — see [`in-process.md`](in-process.md).

## What a new backend owes

There is no shared backend base class and there is no conformance suite a backend inherits by subclassing — `maf_sandbox.testing` is a set of fakes, not a harness, and backends do not import it; each fakes its own provider seam. What a new package is held to instead, assembled from what the shipped ones are actually held to: that `isinstance(backend, SandboxBackend)` holds, the cheapest possible regression test for a renamed method; that the four declarations are asserted, **including the router interaction in both directions** — construction refused at the default floor and admitted below it, `egress` in each configured mode, `ensure_can_serve` admitting a spec it can serve and refusing one it cannot; that every top-level import is standard library, the package itself, or a declared dependency; that no module imports `agent_framework`, since a backend must be usable by a host that does not run the framework at all; and that a backend declaring `FILES_OUT` passes `maf_sandbox.conformance` against a real instance rather than against a fake that agrees with it.

## Where to read next

- [`../capabilities.md`](../capabilities.md) — what each capability obligates, and the caps and confinement rules the pull surface enforces.
- [`../network.md`](../network.md) — the egress axis, the allowlist, and the proxy topology two of these backends share.
- [`../hosts.md`](../hosts.md) — what a host wires, and what identity each declaration implies.
- [`../kinds/README.md`](../kinds/README.md) — the workloads that run on all of this.
- [`../research/sandbox-architecture.md`](../research/sandbox-architecture.md), [`../research/files-out.md`](../research/files-out.md), [`../research/docker-backend-proposal.md`](../research/docker-backend-proposal.md) — the records these pages are distilled from.

## Status

| Decision | State | Tracking |
|---|---|---|
| Four backends declaring against one protocol | shipped | per-backend state on [acas](acas.md), [docker](docker.md), [wslc](wslc.md), [in-process](in-process.md) |
| `_resolve` picks a backend **per spec** (floor ∧ capabilities ∧ egress) rather than one at construction | open | [#328](https://github.com/sokolaidev/maf-extensions/issues/328) open |
| Shared egress probes — every backend declares `ALLOWLIST`, one has been seen enforcing it | open | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) open |
| base64-over-exec as one reviewed implementation in `maf_sandbox` | open — convenience only, never the contract | untracked |
| A fourth `getattr`-read declaration collapses all of them into one declarations object | open — three is where the pattern stops | untracked |
