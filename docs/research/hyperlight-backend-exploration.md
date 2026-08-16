# Hyperlight, read from the source — and whether it can back this suite

Issue #371 asked what `agent-framework-hyperlight` actually is, and said nobody could tell
whether "support Hyperlight" is a week or a quarter until that was settled. This note settles
the factual half by reading the source, and then maps what was found onto this suite's
backend contract. The design decisions #371 names stay decisions; what changes is that three
of its open questions now have answers.

**What was read (2026-08-16):** the `agent-framework-hyperlight` Python package at
`python/packages/hyperlight` in microsoft/agent-framework (checkout `7aa29c64c`, 2026-08-14;
package version `1.0.0b260730`), and the installed stack beneath it: `hyperlight-sandbox`
0.4.0, `hyperlight-sandbox-backend-wasm` 0.4.0 (native module, inspected at string level),
and `hyperlight-sandbox-python-guest` 0.5.0. The .NET twin
(`dotnet/src/Microsoft.Agents.AI.Hyperlight`) exists and was not investigated.

## The stack is four layers

| Layer | What it is | Size |
|---|---|---|
| `agent_framework_hyperlight` | Pure-Python MAF integration: a `ContextProvider` and an `execute_code` `FunctionTool` | ~1,700 lines |
| `hyperlight_sandbox` | Pure-Python facade, deliberately stable: `Sandbox`, `register_tool`, `allow_domain`, `run`, `snapshot`, `restore`, `get_output_files`, `output_path` | 234 lines |
| `hyperlight_sandbox_backend_wasm` | PyO3 Rust module embedding the `hyperlight-wasm` crate, wasmtime 36, wasmtime-wasi, wasmtime-wasi-http, and hypervisor drivers for KVM, MSHV3 and WHP | 9 MB binary |
| `hyperlight-sandbox-python-guest` | CPython 3.14 compiled to wasm32-wasi as a WASI p2 component via componentize-py, with a `call_tool` builtin baked in | 19 MB wasm, 44 MB AOT |

The bottom layer is a real hardware micro-VM: without KVM, WHP or MSHV it fails with "No
hypervisor was found". Wheels exist for Linux x86_64 and Windows AMD64 only, and the wasm
backend is capped below Python 3.14 on the host side.

The second layer is the answer to #371's question **"does the Python package expose the
sandbox beneath its provider as public API at all?"** — yes. `hyperlight-sandbox` is its own
package with a facade whose docstring promises stability while backends and guests move
separately. An adapter can depend on it directly and skip `agent-framework-hyperlight`
entirely, which is the direction #371 already leaned for the opposite reason (the CodeAct
wrapper is the layer this suite replaces).

## How one execution flows

Sandbox creation is cached and snapshot-based. The wrapper keys cached sandboxes by the full
configuration **including a content signature** (relative path, size, mtime_ns) of the
workspace and every mount — editing an input file silently produces a new sandbox. Creating
one: make host temp input/output dirs, stage inputs by copying, construct the `Sandbox`,
register tools, register allowed domains, run `"None"` once to warm the interpreter, then
`snapshot()`. Every `execute_code` call after that is `restore(snapshot)` → clear the output
dir → `run(code)` → collect stdout, stderr and output files. Each call gets a fresh,
pre-warmed interpreter; nothing persists between calls.

Details that matter for an adapter:

- **Host tools.** The guest's `call_tool(name, **kwargs)` crosses the VM boundary as a
  synchronous FFI call into a registered host callback. The wrapper bridges that to async
  `FunctionTool.invoke` with a thread per call. Tools must be registered **before the first
  run** and cannot be unregistered; callbacks live as long as the sandbox.
- **Files.** `/input` and `/output` in the guest are WASI-mapped host temp directories. The
  guest does not push files anywhere: it writes to `/output`, and the *wrapper* collects from
  the host-side dir after the run and attaches the bytes to the tool result.
  `Sandbox.output_path()` returns that host dir; `get_output_files()` lists names.
- **Network.** `allow_domain(target, methods)` is enforced natively at the wasi-http outbound
  boundary, per target and HTTP method, with a hard cap on entry count. Schemeless targets are
  registered as both `http://` and `https://` via a rebuild-and-retry path.
- **Approval.** The whole `execute_code` call is approval-gated if the base mode or *any*
  registered tool requires approval. Never per-`call_tool`. (Same conclusion this suite
  reached independently.)
- **Limits.** 10 MiB max code size. Guest memory defaults: 400Mi heap / 200Mi stack on
  Windows, 25Mi / 35Mi on Linux.

## Two findings that are constraints, not features

**The PyO3 object is thread-confined.** The native `WasmSandbox` is declared `unsendable`:
touching it — or even garbage-collecting it — on any thread other than the one that created
it is an uncatchable Rust panic. The wrapper solves this with a single-thread actor per
sandbox: the sandbox and snapshot never leave the worker thread, and exception tracebacks are
stripped on the worker so unsendable objects cannot leak out through frame locals. Any
adapter this suite writes inherits the same obligation. Our backends are async and already
hop to threads for blocking work; the hop must land on *the same* dedicated thread every
time, for the sandbox's whole life including disposal.

**The wrapper's hardening is host-side path confinement — the same duty ours is.** Roughly a
third of the wrapper and most of its 2,100-line test suite is symlink, junction and
reparse-point defence on the staging and collection paths, including `O_NOFOLLOW` plus a
dev/inode identity check to close the TOCTOU window on output reads. This is the duty
`maf_sandbox.paths.refuse_symlinked_parents` and the conformance suite already own on our
side. An adapter should discharge it with our walk, not import theirs — but their test
catalogue is a ready-made checklist of the attacks the walk must survive.

## What this settles for #371

1. **The `:145` table row's egress is wrong.** `allowed_domains` is a method-scoped
   allowlist enforced natively. The declaration is `ALLOWLIST`; `closed` is only the
   zero-config default. (#371 flagged exactly this for checking.)
2. **The sandbox is public API** — `hyperlight-sandbox`, with a stability promise. The
   dependency question answers itself in favour of dropping `agent-framework-hyperlight`.
3. **The `FILES_OUT` push-vs-pull mismatch is softer than stated.** The guest writes to a
   WASI-mapped host directory; nothing is pushed. `output_path()` gives the host dir, so an
   adapter can serve genuine pull-style `stat_file` / `read_file` / `list_dir` against real
   host paths, with our own confinement walk. The caveat: the wrapper clears the output dir
   on every restore, so the pull window is one run — which is exactly when a kind pulls.

## The fit against our contract

What a `HyperlightSandboxBackend` could declare honestly, from what the source shows. The
table describes the one shipped member of the family — `backend="wasm"` — because the
adapter is a family, not a single backend; the next section says what that means.

| Axis | Declaration | Basis |
|---|---|---|
| `isolation` | `microvm`, **pending the four-point conformance bar** | Real hypervisor (KVM/WHP/MSHV), no guest OS, no reachable token endpoint in a wasm guest. The two-axis doc's micro-VM standard still has to be checked point by point, live. |
| `egress` | `ALLOWLIST` | wasi-http native enforcement, per target and method |
| `capabilities` | `RUN_CODE, HOST_TOOLS, FILES_IN, FILES_OUT, NETWORK, SNAPSHOT` | See per-capability notes below |
| `limits` | Real ceilings exist to declare | 10 MiB code, guest heap/stack, network entry cap |

Per capability, with the honest caveats:

- **`RUN_CODE`** — the natural fit: `Sandbox.run(code)` evaluates Python in the guest with no
  shell anywhere. Still blocked suite-side: `RUN_CODE` gates no protocol method (#371 item 1),
  and the codeact kind hard-requires `EXEC` (#371 item 2, `_tool.py` writes `program.py` and
  execs `python3 program.py`).
- **`EXEC`** — cannot be served. No shell, no argv, no processes in the guest. The backend
  raises from `exec`; the attach gate refuses `EXEC`-requiring specs first. This is the
  established raise-from-ungated-method pattern, not a hack — but `EXEC` sits in
  `DEFAULT_CAPABILITIES`, so every spec that declares nothing refuses this backend. That is
  correct behaviour, worth stating so nobody reads it as a bug.
- **`HOST_TOOLS`** — the native `call_tool` FFI is the concrete second transport #369 could
  only hypothesise about, and the case for its option (c): the backend supplies the channel.
  `dispatch_over_exec` needs `sh`, `nohup` and a pollable filesystem; this guest has none and
  needs none. Two lifecycle facts constrain the adapter: registration is
  before-first-run-only, and the dispatch is synchronous FFI, so our async
  `HostToolRun.dispatch` needs a per-call bridge thread (the wrapper's own pattern).
- **`FILES_IN`** — implementable at acquire time by staging into the sandbox's `input_dir`.
  Whether `write_file` *after* creation is visible in the guest — WASI-mapped dirs suggest
  yes, snapshot/restore semantics suggest maybe not — is the top item for a live probe, not
  an assumption. If restore rolls back guest-visible filesystem state, per-call `write_file`
  forces a re-snapshot and the warm-reuse economics change.
- **`FILES_OUT` / `FILES_LIST`** — implementable as a genuine pull surface against
  `output_path()` on the host, with `refuse_symlinked_parents` walking real host paths. No
  false declaration needed. Scope caveat: the readable tree is `/output`, not an arbitrary
  `work_dir`; the adapter's spec story has to reconcile our `work_dir`-relative path grammar
  with a guest whose writable world is one directory.
- **`SNAPSHOT`** — real and load-bearing: warm restore per call is the package's whole
  performance model.
- **`ATTACHED_IDENTITY`** — no. Nothing in the stack mints identity, which for the isolation
  claim is a feature.

Suite-side, the #371 blockers stand unchanged: `RUN_CODE` gating (#371/#369), the codeact
kind's flat `EXEC` requirement, the pluggable host-tool transport (#369), the platform-axis
gap (#111 — the guest is Python-in-wasm, not a POSIX userland), and one-backend-at-construction
routing (#328/#329), which stops being latent the day a shell backend and a runtime backend
are both registered.

## Hyperlight's own backends: a family, not a proxy

`hyperlight_sandbox.Sandbox(backend=...)` selects an inner native backend of Hyperlight's
own: `"wasm"` is shipped, and `"hyperlight-js"` is defined in the facade and not yet
published. So the adapter this suite would write is not one backend with one set of
declarations — it is a backend *family*, the shape the two-axis table already names for mxc:
declarations derive from the configured containment.

The mechanism follows from patterns already in the suite. The wslc backend's `egress`
property is a declaration computed from configuration — allowlist with a proxy image, closed
without — fixed per instance. The adapter does the same one level up: its constructor takes
the inner backend choice, resolves every declaration from a table keyed by it, and refuses a
choice with no entry, mirroring `_normalize_backend`'s own refuse-unknown. Resolved at
construction and static afterward, which is what the router's construction-time floor check
assumes.

What the inner choice actually moves is less than the word "backend" suggests. Both members
sit on the same boundary — the hyperlight VMM over KVM, WHP or MSHV — so across today's
family the isolation rung does not vary: both would claim `microvm`. What varies is
everything else: the language `RUN_CODE` evaluates (the #111 platform axis, sharpened),
whether egress enforcement exists at all (wasmtime-wasi-http is a fact about the wasm
backend, promised nowhere for the js one), the host-tools FFI, and the limits. The
declaration table carries isolation per entry anyway, because that is the mechanism that
catches a future member whose boundary is different — any no-hypervisor mode above all.
Upstream Hyperlight historically had an in-process debug mode; this build's strings say
boot-or-refuse ("No hypervisor was found"), which is the right failure direction, and
confirming no fallback is reachable is on the probe list below.

One reading of "family" is wrong and worth refusing in advance: an adapter that picks an
inner backend per spec at acquire time. That moves the selection below the policy layer that
owns refusals — every router check reads the backend's declarations as facts about *the*
backend — and forces the adapter to declare the weakest value across its options to stay
honest. A host that wants two runtimes registers two configured instances, and choosing
between them per spec is the router's job, which lands on #328/#329 with a third backend
shape in play rather than two.

The rung is earned per entry, not inherited from the family name: `microvm` is a conformance
bar, so (wasm × WHP) gets an entry when the four points are checked there, (wasm × KVM) is
its own confirmation, and hyperlight-js gets no entry until it exists and passes.

## What reading cannot answer — the live-probe list

1. Does a host-side write into `input_dir` after creation appear in the guest, and does it
   survive `restore`? (Decides the whole `FILES_IN`/`write_file` story.)
2. Does the micro-VM standard's four-point bar hold, point by point?
3. Cold-start and restore latency, and guest memory headroom under the Windows 400Mi default
   — the numbers that decide whether warm reuse is a nicety or a requirement.
4. Whether `run()` honours a timeout at all — `Sandbox.run` takes none, and our `exec`
   contract is timeout-bounded. An unbounded guest loop may hold the worker thread forever
   ("GuestFunctionCallAlreadyInProgress" strings in the native module hint at the failure
   mode).
5. The conformance suite's applicability to a backend that raises from `exec` — what subset
   of probes a `RUN_CODE`-shaped backend must pass.
6. Whether any no-hypervisor or in-process fallback is reachable from the Python stack.
   Upstream Hyperlight had a debug mode of that shape; one reachable here would collapse the
   `microvm` claim, so its absence is verified rather than assumed.

## Verdict

Yes — the package can back a backend, and the right dependency is `hyperlight-sandbox`
directly (0.4.x, pre-1.0: pin tightly). It slots in as the suite's first runtime-shaped
backend: `microvm` isolation with `ALLOWLIST` egress, serving `RUN_CODE` workloads, refusing
`EXEC` ones honestly at attach. Nothing found in the source contradicts #371's sequencing —
the suite-side prerequisites still come first — but the source removes the feasibility
unknowns: the API surface is public and stable-by-intent, the egress is allowlist-grade, the
pull pair is implementable without a false declaration, and the two real costs are named
(thread confinement, platform envelope) rather than lurking.
