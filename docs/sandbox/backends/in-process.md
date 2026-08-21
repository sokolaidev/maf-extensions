# `in-process` — the testing fake

> `maf_sandbox.testing`: a real implementation of both protocols that runs the workload in this process, with every declaration constructor-overridable. It is how a kind and a policy get tested offline, and it is the one backend whose honesty *is* its contract. Shipped in [`packages/maf-sandbox/README.md`](../../../packages/maf-sandbox/README.md).

## What it declares

| Declaration | Default | Overridable |
|---|---|---|
| `isolation` | `Isolation.NONE` | yes |
| `capabilities` | `DEFAULT_CAPABILITIES` — `{EXEC, FILES_IN}` | yes |
| `egress` | `Egress.ALLOWLIST` | yes |
| `limits` | `DEFAULT_SANDBOX_LIMITS` | yes |

`Isolation.NONE` is the whole point: this backend runs nothing in a boundary. The workload executes in the host process with the host's authority, and the declaration says exactly that, so the router's default `microvm` floor refuses it and a host that wants it opts all the way down. Nothing here pretends otherwise, and that is what makes it safe to ship in the wheel.

`egress` defaults to `ALLOWLIST` rather than to silence so a workload under test **attaches** as it would against a live backend, instead of every offline test becoming a test of the attach refusal. `capabilities` still defaults to `DEFAULT_CAPABILITIES` even though the sandbox genuinely implements the pull surface: widening the default would change what a bare `InProcessSandboxBackend()` attaches against for every existing caller that never asked for `FILES_OUT` or `FILES_LIST`. A test that wants the pull surface asks for it.

## Overridable declarations are what make it a policy fixture

Every one of the four is a constructor argument, and that is not a convenience — it is the feature. The router's minimum-isolation floor is exercised against fakes claiming *every* rung on the ladder, not only `NONE`; `selected=` is exercised against several registered backends distinguished by `name`; the capability match, the egress-honesty rule and the transfer-limit match each need a backend that declares the thing under test. No other backend can be made to declare a rung it does not have, and none should be able to. See [`../policy-isolation.md`](../policy-isolation.md).

## What it records, and the degrade path

Every `acquire` records its `key` into `keys` and its `spec` into `specs`; every `dispose` records into `disposed`; every `dispose_scope` records `(scope, thread_id)` into `purged` and returns a settable `purge_count`, so a test can simulate more than one sandbox reclaimed. `exec` records `(command, working_directory, timeout)` into `commands`, joining an argv with `shlex.join` first so a marker written against a string matches an equivalent argv command the same way. **`acquire_error`** makes `acquire` raise instead of returning, which is how a kind's "sandbox unavailable" degrade path gets exercised — the branch that returns the workload to T0 visibly, and the one nothing else can reach offline. A failed acquire records nothing, because it acquired nothing.

One deliberate simplification: every `acquire` returns the same sandbox whatever the key or the spec's kind, where a real backend keys sandboxes by `(key, kind)`. A test that cares which kind asked reads `specs`; a test needing two genuinely distinct sandboxes registers two backends.

## The protocol surface it implements

The fake implements the whole `Sandbox` protocol, because a member it did not implement would be a member no kind's test suite could exercise: `write_file` ([`testing.py:168`](../../../packages/maf-sandbox/src/maf_sandbox/testing.py)), `exec` (`:172`), `stat_file` (`:219`), `read_file` (`:230`), `remove` (`:248`) and `list_dir` (`:280`). Storage is bytes, keyed by normalised absolute guest paths, so it can stand in for a real pull surface rather than only for a text-only one; `seed_files` plants regular content, and `EntryKind.SYMLINK`, `EntryKind.DIRECTORY` or any other kind plants an entry with no content. All four read methods confine `path` to the `working_directory` a call names and run the shared `refuse_symlinked_parents` walk over the components, the same rule a real backend enforces against its own guest filesystem. `read_file` serves only `EntryKind.FILE` and **refuses** rather than truncates a file over `max_bytes`.

## Shape, not safety

It answers the conformance probes, and what a green means here is narrower than elsewhere: **a seeded link has no target**, so nothing ever reads through one and a passing run has asserted shape, not safety. It refuses a link standing where a directory was expected because the walk classifies it, not because the escape was attempted and failed. That is why both real backend suites carry their own premise test — that the provider genuinely resolves through a link, so the refusals are refusing something reachable — and why the probes are also run against a real engine and a real service. See [`../capabilities.md`](../capabilities.md) § "`maf_sandbox.conformance` is the executable spec".

## The reach-by-name hazard

`maf_sandbox.testing` is **not re-exported from `__init__`**, and the placement is the warning. Importing it in production code is the foreseeable mistake — a fake that declares `Egress.ALLOWLIST` and enforces nothing, running the workload in the host process with the host's credentials, while every router check passes — so the module is reachable only by an import someone has to write on purpose and a reviewer can see. That is the same rule `maf_sandbox.paths` and `maf_sandbox.conformance` sit behind, and the criterion is a hazard rather than an audience: see [`../architecture.md`](../architecture.md) § "Where shared code lives".

## Status

| Decision | State | Tracking |
|---|---|---|
| One supported fake for both protocols, replacing the per-suite hand-rolled ones | shipped | — |
| All four declarations constructor-overridable; the full pull surface implemented | shipped | — |
| The conformance probes answer as shape, not safety — a seeded link has no target | by design | — |
| Reaching for `maf_sandbox.testing` in production | mitigated by by-name placement, never prevented | untracked |
