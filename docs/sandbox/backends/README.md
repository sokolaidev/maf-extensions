# Backends

> What each shipped backend declares to the router, what it does with a sandbox's life, and where it is honestly quirky.

## The boundary these pages keep

A backend has two audiences and they want different documents. These pages own the **architecture-facing contract**: the declarations the router reads, lifecycle behaviour, conformance status, and the quirks a kind author is entitled to know before choosing where a workload runs. Each package's own README owns **installation, configuration fields and usage**, and is linked from its page here rather than copied into it — a sentence that lives in both places is a sentence that will drift, and the copy nobody edits is the one somebody reads.

## The five declarations

Every backend answers five questions before a workload's tool is ever attached, and the router acts on each one. **`isolation`** is a rung on an ordered ladder, checked against the stricter of the host's floor and the spec's; below it, construction raises rather than degrading. It is a required member of the `SandboxBackend` protocol, and the other four are read off the backend with `getattr`, so a backend written before one of them existed keeps loading — with its silence read differently in each case. **`capabilities`** is a frozenset matched against the spec's `requires`, and silence is read charitably as `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what `Sandbox` already obligates. **`egress_modes`** is the set of `Egress` modes the backend can *enforce*; a spec runs in exactly one mode, and the router serves it only when that mode is in the set and refuses otherwise — never substituting a more open one (which would silently widen what the workload reaches) or a more isolated one (which would hand it a posture it was not built for). Silence there is the empty set: a backend that declares nothing enforces nothing, so every ask is refused. **`limits`** declares transfer ceilings per direction and follows the egress rule rather than the capability one — a cap is a safety claim, so silence resolves to `DEFAULT_SANDBOX_LIMITS`, never to "no ceiling". **`os_families`** names the guest shapes the backend hands out, matched against a spec's `requires_os_family`; its silence is neither charitable nor conservative but the **absence of an answer** — `frozenset()`, which refuses a spec that asks and leaves every spec that does not exactly as it was. The checks themselves, and who owns each, are in [`../policy-isolation.md`](../policy-isolation.md); the egress model is [`../network.md`](../network.md).

`os_families` is the **fourth** `getattr`-read declaration, and the protocol's own docstring named a fourth as the signal to collapse all of them into one declarations object. The signal has fired; the collapse is owed and unbuilt, because it rewrites every backend's declaration surface at once.

## The shipped backends

| | [`acas`](acas.md) | [`docker`](docker.md) | [`wslc`](wslc.md) | [`in-process`](in-process.md) |
|---|---|---|---|---|
| **`isolation`** | `microvm` — a hardware-isolated micro-VM, a property of the Azure service rather than of this code; meets the router's default floor with nothing configured | `container` — a constant no configuration raises; a host opts the floor down explicitly | `container` — the same opt-down, for the same reason | `none` — and it runs nothing in a boundary at all |
| **`capabilities`** | `EXEC, FILES_IN, FILES_OUT, FILES_LIST, HOST_TOOLS` — the only backend that can enumerate, which is the capability split's own test applied to itself | `EXEC, FILES_IN, FILES_OUT, FILES_DELETE, HOST_TOOLS` — never `FILES_LIST` | `EXEC, FILES_IN` | `DEFAULT_CAPABILITIES`, though the sandbox implements more than it declares |
| **`egress_modes`** | `{allowlist, closed}` — one Deny-default policy at two settings: the hosts the spec names, or nothing. Never `unrestricted` | `{closed}`; `{closed, allowlist}` when an egress proxy image is configured | the same two shapes, by the same topology | `{allowlist, closed}` by default, constructor-overridable |
| **`limits`** | declared — 32 MiB per file, 128 MiB total, 128 files, each direction | declared — 64 MiB per file, 256 MiB total, 256 files, each direction | **not declared** — silence resolves to `DEFAULT_SANDBOX_LIMITS` | `DEFAULT_SANDBOX_LIMITS` |
| **`os_families`** | **not declared** — every sandbox it hands out is Linux, and nothing states it | **not declared** | **not declared** | `frozenset()` by default, constructor-overridable |
| **Identity** | the control-plane credential never travels into the guest; no path from inside back to the host's identity or another conversation's sandbox | none attached | none attached | the host's own process, with the host's authority — which is what `none` says |
| **Reuse & purge** | warm resume over cold create; labels at create, purge by label from the service; get-or-create serialised per key | reuse, restart, adopt-on-name-conflict; labels at create, purge by label from the engine | sub-second creates; egress scaffolding re-ensured on every acquire; labels and label purge | records every key, spec, dispose and purge; `acquire_error` for a kind's degrade path |
| **Where it runs** | anywhere with the service reachable | macOS, Linux, Windows with WSL 2, and CI runners | Windows with WSL only | this process |

**No shipped backend declares `RUN_CODE`, and none declares a guest family.** `run_code` is a `Sandbox` method rather than an optional extra, so all four implement it — three of them as a refusal that names the backend and the reason, the fake as scripted output.

Every cell above is read from each backend's `_backend.py`, which is the source of record; the router's own view of the same rows is [`../policy-isolation.md`](../policy-isolation.md) § "The map", and what each capability obligates is [`../capabilities.md`](../capabilities.md).

## What every backend is held to

**Declare honestly or not at all.** There is no router-side emulation of a capability a backend lacks, and there must not be: the whole value of the match is that a refusal at attach means the workload would genuinely not have run. A mechanism can exist and stay undeclared — ACAS implements `remove` and withholds `FILES_DELETE` — and that asymmetry is the safe direction.

**A protocol method a backend cannot serve raises, and says why.** `Sandbox` is `runtime_checkable`, so a member it omitted would stop it being a `Sandbox` at all — which is why an unservable method refuses rather than being left out. `run_code` is the newest of them: acas, docker and wslc each raise `NotImplementedError` naming the backend and the reason — *which* runtime an image carries is a property of the image, and none of the three parses the reference it was handed, so declaring `RUN_CODE` would be a claim about someone else's artefact. The router refuses a spec requiring the capability before any caller arrives, so the raise is the honest floor under a caller that skipped the check, exactly as it is for `remove` and `list_dir`.

**Purge consults the service, never process memory.** A conversation delete lands on whichever replica serves it, which is usually not the replica that created the sandbox, so a backend that sweeps its own dictionary leaks billable compute on every multi-replica host. Labels are written **at create**, so a sandbox is reachable by the identity a later purge will select on even if everything after the create fails. Label values are **hashed rather than truncated** when they will not pass through intact: two scopes sharing a prefix would land on the same label, and one conversation's purge would then delete another's sandboxes. The mapping has to be identical on write and on query, or the purge quietly selects nothing — which looks exactly like a clean tenant.

**The acquire race is real and invisible on one machine.** The function calls in a single assistant message execute concurrently, so two acquires for one key can be in flight at once. A backend either serialises its get-or-create or derives a name the provider rejects duplicates of; an unguarded read-then-create hands out two sandboxes and remembers one. The shipped backends do both where they can — a per-key lock, plus an adopt-on-name-conflict path for the race that lives in another process and no lock can see.

**base64-over-exec is opt-in convenience, never the contract.** It depends on `base64` and a shell existing in the image, which the native copy paths do not. If it ever ships it is one reviewed implementation in `maf_sandbox`, never a parse per backend, and it carries its own lower maxima.

**A backend claiming `microvm` *and* `FILES_OUT` owes an extra clause.** The declared channel is reads confined to the working directory with non-regular entries refused — the micro-VM standard's fourth leg applied to the out-door. See [`../policy-isolation.md`](../policy-isolation.md) § "The micro-VM standard".

**`maf_sandbox.testing` grows every protocol surface, or no kind can be tested.** The in-process fake is not an optional convenience: a kind is written against the protocol and nothing else, so a protocol member the fake does not implement is a member no kind's test suite can exercise offline. It implements stat, read, list and remove today and declares capabilities configurably — see [`in-process.md`](in-process.md).

## What a new backend owes

There is no shared backend base class and there is no conformance suite a backend inherits by subclassing — `maf_sandbox.testing` is a set of fakes, not a harness, and backends do not import it; each fakes its own provider seam. What a new package is held to instead, assembled from what the shipped ones are actually held to: that `isinstance(backend, SandboxBackend)` holds, the cheapest possible regression test for a renamed method; that the five declarations are asserted, **including the router interaction in both directions** — construction refused at the default floor and admitted below it, `egress_modes` in each configured shape and a spec refused for a mode outside it, `ensure_can_serve` admitting a spec it can serve and refusing one it cannot; that every top-level import is standard library, the package itself, or a declared dependency; that no module imports `agent_framework`, since a backend must be usable by a host that does not run the framework at all; and that a backend declaring `FILES_OUT` passes `maf_sandbox.conformance` against a real instance rather than against a fake that agrees with it.

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
| `egress_modes` replaces the single `egress` property: a backend declares the set it can enforce, and the router serves the spec's mode or refuses it | shipped | [#524](https://github.com/sokolaidev/maf-extensions/issues/524) (closed) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed); [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) |
| `run_code` is a `Sandbox` method every shipped backend answers | shipped — all three real backends answer by refusing, and none declares `RUN_CODE` | [#531](https://github.com/sokolaidev/maf-extensions/pull/531) (merged) and [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged), closing [#381](https://github.com/sokolaidev/maf-extensions/issues/381) (closed) |
| `os_families`, matched against a spec's `requires_os_family` | shipped in core, **declared by nobody** — no shipped backend states a guest family, so the axis refuses only a spec that asks | [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged), closing [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) |
| `_resolve` picks a backend **per spec** (floor ∧ capabilities ∧ egress) rather than one at construction | open | [#328](https://github.com/sokolaidev/maf-extensions/issues/328) open |
| Shared egress probes — every backend that can enforce `ALLOWLIST` declares it, one has been seen enforcing it | open | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) open |
| base64-over-exec as one reviewed implementation in `maf_sandbox` | open — convenience only, never the contract | untracked |
| The four `getattr`-read declarations collapse into one declarations object | open — `os_families` is the fourth, so the signal the protocol names has fired | untracked |
