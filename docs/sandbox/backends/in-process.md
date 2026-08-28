# `in-process` — the testing fake

> `maf_sandbox.testing`: a real implementation of both protocols that runs the workload in this process, with every declaration constructor-overridable. It is how a kind and a policy get tested offline, and it is the one backend whose honesty *is* its contract. Shipped in [`packages/maf-sandbox/README.md`](../../../packages/maf-sandbox/README.md).

## What it declares

The four below `isolation` are fields of one `declarations`, overridden together by passing a `BackendDeclarations` to the constructor.

| Declaration | Default | Overridable |
|---|---|---|
| `isolation` | `Isolation.NONE` | yes |
| `capabilities` | `DEFAULT_CAPABILITIES` — `{EXEC, FILES_IN}` | yes |
| `egress_modes` | `{Egress.ALLOWLIST, Egress.CLOSED}` | yes |
| `limits` | `DEFAULT_SANDBOX_LIMITS` | yes |
| `os_families` | `frozenset()` | yes |

`Isolation.NONE` is the whole point: this backend runs nothing in a boundary. The workload executes in the host process with the host's authority, and the declaration says exactly that, so the router's default `microvm` floor refuses it and a host that wants it opts all the way down. Nothing here pretends otherwise, and that is what makes it safe to ship in the wheel.

`egress_modes` defaults to `{ALLOWLIST, CLOSED}` rather than to silence so a workload under test **attaches** as it would against a proxy-capable live backend: the default `CLOSED` spec and an `ALLOWLIST` spec both resolve, instead of every offline test becoming a test of the attach refusal. A test *of* the refusal passes a narrower set: `frozenset()` for a backend that enforces nothing, `{UNRESTRICTED}` for the no-confinement shape — which is what the no-isolation backend in [`samples/09_inprocess_bicep`](../../../samples/09_inprocess_bicep) now declares, honestly, and it is served only by a workload that asked to run open.

`capabilities` still defaults to `DEFAULT_CAPABILITIES` even though the sandbox genuinely implements the pull surface: widening the default would change what a bare `InProcessSandboxBackend()` attaches against for every existing caller that never asked for `FILES_OUT` or `FILES_LIST`. A test that wants the pull surface asks for it. `os_families` defaults to `frozenset()` — exactly what the router reads from a backend that declares nothing, so a test written before the axis existed is unaffected and one exercising it states a family. `FAKE_BACKEND_DECLARATIONS` is the whole default object, and `egress_modes` is the one field it departs from `DEFAULT_BACKEND_DECLARATIONS` on.

## Overridable declarations are what make it a policy fixture

Every one of the five is a constructor argument, and that is not a convenience — it is the feature. The router's minimum-isolation floor is exercised against fakes claiming *every* rung on the ladder, not only `NONE`; `selected=` is exercised against several registered backends distinguished by `name`; the capability match, the egress resolution, the guest-family match and the transfer-limit match each need a backend that declares the thing under test. No other backend can be made to declare a rung it does not have, and none should be able to. See [`../policy-isolation.md`](../policy-isolation.md).

## What it records, and the degrade path

Every `acquire` records its `key` into `keys` and its `spec` into `specs`; every `dispose` records into `disposed`; every `dispose_scope` records `(scope, thread_id)` into `purged` and returns a settable `purge_count`, so a test can simulate more than one sandbox reclaimed. **`dispose_failure`** and **`purge_failure`** make either report a delete that failed *without* raising — what a real backend does, since both are contractually best-effort — which is how the router's refusal ledger gets exercised. `exec` records `(command, working_directory, timeout)` into `commands`, joining an argv with `shlex.join` first so a marker written against a string matches an equivalent argv command the same way. `reclaim` records `(directory, working_directory, timeout)` into **`reclaims`** and really removes the directory and every descendant from the store, so a test asserting that a call cleaned up after itself has a target of its own — a list rather than a line in `commands`, for the reason `programs` is one: a removal must not be satisfied by a shell command that happens to contain the same text, and a cleanup that stopped happening must fail a test rather than quietly assert nothing. **`acquire_error`** makes `acquire` raise instead of returning, which is how a kind's "sandbox unavailable" degrade path gets exercised — the branch that returns the workload to T0 visibly, and the one nothing else can reach offline. A failed acquire records nothing, because it acquired nothing.

One deliberate simplification: every `acquire` returns the same sandbox whatever the key or the spec's kind, where a real backend keys sandboxes by `(key, kind)`. A test that cares which kind asked reads `specs`; a test needing two genuinely distinct sandboxes registers two backends.

## The protocol surface it implements

The fake implements the whole `Sandbox` protocol, because a member it did not implement would be a member no kind's test suite could exercise: `write_file` ([`testing.py:180`](../../../packages/maf-sandbox/src/maf_sandbox/testing.py)), `exec` (`:184`), `run_code` (`:196`), `stat_file` (`:247`), `read_file` (`:258`), `remove` (`:276`), `reclaim` (`:308`) and `list_dir` (`:332`). Storage is bytes, keyed by normalised absolute guest paths, so it can stand in for a real pull surface rather than only for a text-only one; `seed_files` plants regular content, and `EntryKind.SYMLINK`, `EntryKind.DIRECTORY` or any other kind plants an entry with no content. All four read methods confine `path` to the `working_directory` a call names and run the shared `refuse_symlinked_parents` walk over the components, the same rule a real backend enforces against its own guest filesystem. `read_file` serves only `EntryKind.FILE` and **refuses** rather than truncates a file over `max_bytes`.

`reclaim` is the member the fake cannot answer with a gesture, since no capability gates it and nothing else offline stands in for it. It removes the directory and everything under it from the store for real, so a kind's test sees the state a real backend would leave behind; and it records the call into `reclaims`, so a test that asserts the framework reclaimed a call's directory asserts something that fails when the reclaim stops happening. Confinement is no more its duty here than it is a real backend's: what it is handed is a directory the framework created under `working_directory`.

`run_code` is the one member that **scripts** rather than refusing, where all three real backends raise. A fake that refused would make every kind written against `run_code` untestable without a real backend, which is the one thing this class exists to avoid. Nothing is evaluated: `outputs` is matched against the program text as a substring, exactly as it is matched against a command line, and each call is recorded into `programs` as `(code, timeout)` — a list of its own rather than `commands`, so a test asserting a program was evaluated cannot be satisfied by a shell command that happens to contain the same text. The backend still declares no `RUN_CODE` by default, so a spec requiring it is refused at attach unless a test asks for the capability explicitly.

## Shape, not safety

It answers the conformance probes, and what a green means here is narrower than elsewhere: **a seeded link has no target**, so nothing ever reads through one and a passing run has asserted shape, not safety. It refuses a link standing where a directory was expected because the walk classifies it, not because the escape was attempted and failed. That is why both real backend suites carry their own premise test — that the provider genuinely resolves through a link, so the refusals are refusing something reachable — and why the probes are also run against a real engine and a real service. See [`../capabilities.md`](../capabilities.md) § "`maf_sandbox.conformance` is the executable spec".

## The reach-by-name hazard

`maf_sandbox.testing` is **not re-exported from `__init__`**, and the placement is the warning. Importing it in production code is the foreseeable mistake — a fake that declares it can enforce `ALLOWLIST` and `CLOSED` and enforces neither, running the workload in the host process with the host's credentials, while every router check passes — so the module is reachable only by an import someone has to write on purpose and a reviewer can see. That is the same rule `maf_sandbox.paths` and `maf_sandbox.conformance` sit behind, and the criterion is a hazard rather than an audience: see [`../architecture.md`](../architecture.md) § "Where shared code lives".

## Status

| Decision | State | Tracking |
|---|---|---|
| One supported fake for both protocols, replacing the per-suite hand-rolled ones | shipped | — |
| All five declarations constructor-overridable; the full pull surface implemented | shipped | — |
| `egress_modes` replaces the single `egress` default, at `{ALLOWLIST, CLOSED}` so an offline test attaches as it would against a proxy-capable backend | shipped | [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| `run_code` scripted rather than refused, recorded into `programs` | shipped — the fake is the only implementation that answers it with a result | [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged), closing [#381](https://github.com/sokolaidev/maf-extensions/issues/381) (closed) |
| `os_families` overridable, defaulting to `frozenset()` | shipped — the same silence the router reads from a backend that declares nothing | [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged), closing [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) |
| `reclaim` is a real in-memory removal recorded into `reclaims`, not a scripted answer | shipped — a fake that only recorded it would let every core reclaim assertion pass while asserting nothing | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) |
| The conformance probes answer as shape, not safety — a seeded link has no target | by design | — |
| Reaching for `maf_sandbox.testing` in production | mitigated by by-name placement, never prevented | untracked |
