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

## Tool calling across the boundary, and the transport design it forces

How a dispatch travels in the Hyperlight stack, end to end:

1. The host registers named callbacks with `Sandbox.register_tool` — **before the first
   `run()`**, held for the sandbox's life, never unregistered. The wrapper does it just
   before the warm-up run, so the tool set is baked into the snapshot every call restores.
2. Model-written guest code calls `call_tool(name, **kwargs)`, a builtin compiled into the
   CPython-in-wasm guest. Keyword arguments only.
3. The call is a synchronous VM exit — a Hyperlight host function. The guest blocks until
   the host returns. The FFI marshals dict, list, str, int, float, bool and None natively,
   and **falls back to `repr()`/`str()` for anything else**, silently and lossily.
4. The callback fires on the sandbox's worker thread, nested inside the blocked `run()`.
   The wrapper bridges sync-to-async with a fresh thread per call running `asyncio.run`.
5. One call at a time; a hung callback hangs the guest, and `run()` has no timeout to cut
   it loose — the native strings name the state (`FunctionHungOnHostFunctionCall`) and no
   recovery.

And the boundary of what Hyperlight supplies: **a wire, and only a wire.** Name lookup is
its entire dispatch policy — no cap, no response ceiling, no argument validation beyond the
tool's own, no integrity or identity axes, approval only on the whole `execute_code` call.
Everything this suite calls the safety contract has to come from our side.

Our side turns out to be ready for that. `HostToolRun.dispatch(name, arguments, *,
framing_bytes) -> DispatchResult` is already transport-neutral — a string and a mapping in,
`value_json` or a sanitized refusal out, every gate behind the one door — and #369 already
names the missing half (negotiation) with three candidate shapes. What the Hyperlight source
adds:

- **The trampoline rule.** The adapter never registers a kind's callables directly (the
  wrapper's model). It registers one trampoline per declared name, and the trampoline's body
  is a call to the current run's `dispatch`. Their FFI is the wire; our dispatch is the
  door. Every gate — the cap with refused calls counted, the byte ledger, validation, the
  USER-identity refusal — comes free.
- **`RUN_CODE` gets its method.** A `run_code(code, *, timeout)` optional method on the
  `Sandbox` protocol, gated by `Capability.RUN_CODE` exactly as `FILES_OUT` gates the
  pull pair (#371 item 1). Host-tool dispatch becomes part of that method's contract.
- **#369 resolves as shape C, and its sequencing gate opens.** The issue held all three
  shapes until #302's measurement, because the second transport it foresaw (streamed stdin)
  depended on it. Hyperlight is a different second transport, arriving for capability
  reasons — so "settled with two real transports in hand" is satisfiable without #302. And
  it argues for C specifically: the kind cannot compose this channel out of `exec`,
  `write_file` and `stat_file`, because none of them exist in the guest. The channel can
  only be the backend's own, which is what shape C says `HOST_TOOLS` should mean.
- **Two absorptions the adapter owes.** The sync FFI callback runs on the worker thread
  while the host loop is free, so the trampoline bridges with `run_coroutine_threadsafe`
  and blocks on the future **with the dispatch deadline**, answering a timeout with a
  refusal envelope rather than hanging the guest. And the guest is handed `value_json` —
  host-side JSON, never raw objects — because serializing at the door is what makes the
  response ceiling enforceable; the FFI's `repr()` fallback is the cautionary tale.
  `framing_bytes` is the FFI envelope's overhead, presumably a small constant (probe item).
- **The fourth-declaration trigger.** A backend-supplied channel is the fourth optional
  declaration on `SandboxBackend`, which `_protocol.py` says is the signal to collapse all
  of them into one declarations object. Shape C lands together with that refactor.

Unchanged by any of this: approval stays whole-call at the kind layer (both stacks reached
that independently), and guest-initiated HTTP back to the host stays rejected — the FFI
removes even the temptation.

## The composition question: what happens to the CodeAct library itself

The question will be asked, so this doc answers it: how does `agent-framework-hyperlight` —
the provider and the `execute_code` tool — compose with the design above? It doesn't, and
that is the design. The package's top two thirds are replaced, the bottom third becomes the
backend, and the experience the library delivers is re-served by this suite's layers:

| What the library does | Where it lands here |
|---|---|
| `HyperlightCodeActProvider` injects instructions + a run-scoped `execute_code` tool | The codeact kind + `sandboxed_tool`: the description is built from the channels the host wired, and nothing attaches when no sandbox is configured |
| `tools=[...]` as bare callbacks, name lookup the only gate | `HostToolRegistry` with declarations, dispatched through the trampoline — cap, ledger, validation, identity refusal at the door |
| The guest's `call_tool` builtin | Kept — it is baked into the guest binary and becomes the transport's guest half. Like the over-exec shim, it is not a control; the gates are host-side |
| `workspace_root` / `file_mounts` staged at creation, content-signature cached | `FILES_IN` per spec — and the program stops being a file at all, see the flow below |
| `allowed_domains`, with per-method restriction | `spec.egress_allow` against the backend's `ALLOWLIST` declaration; the method refinement is #377, additive |
| `/output` collected and attached to the tool result | `DeclaredOutput` + `collect_outputs`: pull against `output_path()`, dispositions, sinks, caps — the model gets references, never bytes |
| `approval_mode`, whole-call | Unchanged; both stacks reached it independently |
| The micro-VM, snapshot/restore, the warm interpreter | The backend — the only part adopted, via `hyperlight-sandbox` directly |

The flow end to end on our stack: at **attach**, the codeact spec declares `RUN_CODE`
rather than `EXEC`, and the router's five checks refuse before anything boots — the step
the provider simply does not have. At **acquire**, the backend registers one trampoline per
sealed registry name and the name set joins the sandbox's cache identity, exactly as the
wrapper keys on tool identity. **Per call**, the kind hands the program text to
`run_code(code, timeout=...)` — no `program.py`, no interpreter invocation, so the false
`python3 program.py` sentence #111 flagged never gets written, and the write-after-create
question (probe item 1) shrinks to data files only. Mid-run, `call_tool` → FFI → trampoline
→ `dispatch`. Afterward, outputs are pulled from the host-side output dir with our
confinement walk and landed through sinks. At **dispose**, the worker-actor teardown wires
into `dispose`/`dispose_scope` so scope reclamation reaches it.

The honest trade for a host author, against using the library directly: gained — attach-time
refusals, declared tools, capped and ledgered dispatches, pulled outputs, and a workload
that also runs on wslc, Docker, or the in-process fake unchanged. Given up — the per-method
egress precision (#377 is the additive axis) and the content-signature sandbox caching,
which explicit `(key, kind)` identity replaces.

And one caveat to write down wherever the adapter ships: nothing stops a host attaching
`HyperlightCodeActProvider` to an agent *beside* this suite, and doing so bypasses every
gate above — no attach refusal, no floor, no identity denial, no caps. That is not a flaw
in either library; it is SANDBOX.md's thesis restated. The other answers become backends
beneath the contract, not alternatives beside it.

## Feature parity with the CodeAct library — the ledger

The headline features are mapped above. What remains is the smaller surface, itemized here
so each is consciously matched, deliberately diverged from, or tracked — never silently
missing. Where the decision was clear, an issue exists.

1. **Between-run mutability.** Their provider mutates live — add/remove tools, mounts and
   domains between runs, the sandbox cache following each change. Our spec is frozen and
   the registry seals, and that is the decision: a configuration change is a new sandbox
   identity on their side too (their cache key says so); the honest equivalent here is
   rebuilding the tool. **Rebuild-the-tool is the supported path.** Stated here so it is a
   rule hosts read rather than a limitation they report.
2. **The guest execution contract, at the prompt level.** Their instructions carry the
   sentences that make models effective: the sandbox does not return the last expression,
   end with `print(...)`; prefer one call; large artifacts go to `/output`. The equivalent
   contract for `run_code` — what it promises the model, per backend — belongs to the
   method's design and the kind's instructions. Folded into #381.
3. **Custom guest modules are family entries, not options.** `module`/`module_path` lets a
   caller swap in any `.wasm`/`.aot` guest. Here that is a declaration hazard: a different
   guest changes what `RUN_CODE` evaluates, whether `call_tool` exists, and what the
   conformance run proved. The rule follows from the family section: the packaged Python
   guest is the table entry that earned its declarations; a custom module is a different
   family member starting with none — refused, or admitted through its own conformance
   pass. Lands with the adapter.
4. **Resource ceilings.** `heap_size`/`stack_size` are real knobs; our `SandboxLimits`
   caps transfers only, and nothing declares memory or CPU. A deliberate non-axis for now
   — backend configuration, not vocabulary — recorded so it reads as chosen. Same shape as
   #377 the day two backends differ in what they can bound.
5. **Per-run effective-state serialization.** Their provider writes the effective config
   into session state every run; we record nothing about the run we served. Clear win,
   no open axes: #380.
6. **How artifacts surface in chat.** Their output files return as inline `Content` bytes
   and render immediately; ours land through sinks as references — the right
   confidentiality posture and a worse demo. The open question is whether reference-only
   is a rule or a default a sink may relax under a size threshold. Undecided, recorded.
7. **Queue time versus timeout.** MAF runs parallel tool calls; a per-sandbox worker
   serializes them, so a call can exhaust its budget waiting behind another. The contract
   must make that refusal distinguishable from the program overrunning. Folded into #381.
8. **The long tail, named to be skipped or matched knowingly:** the `execute_code`
   name-collision guard in tool registration; schemeless-domain expansion to both schemes
   (#377 territory); stdout `\r\n` normalization and the executed-without-output sentinel;
   install-hint errors for missing wheels; the telemetry feature marker; and the .NET twin
   — MAF ships both languages, this suite ships one, a strategic asymmetry rather than a
   feature gap.

Not on the ledger: interpreter-state persistence. Both stacks wipe state per call via
restore; parity is already exact.

## What reading could not answer — measured (2026-08-18)

The seven questions this section used to list were put to the running stack. Where an
answer below contradicts a guess in the sections above, the answer wins; the earlier text
stays as the record of what reading alone could and could not see.

**The environment finding that preceded every probe:** the published wheels are skewed.
`hyperlight-sandbox-python-guest` 0.5.0 ships an AOT compiled with Wasmtime 36.0.11;
`hyperlight-sandbox-backend-wasm` 0.4.0 embeds 36.0.7; the native binding only
deserializes precompiled artifacts, so every `run()` fails and there is no JIT fallback.
`hyperlight-sandbox` 0.4.0 pins the guest `>=0.4.0` with no compatible upper bound, so a
naive install produces a stack that cannot execute at all. Every measurement below is from
the exact matched trio — all three packages at 0.4.0 — on Windows over WHP. "Pre-1.0: pin
tightly" is now a demonstrated failure mode, not advice.

1. **Input visibility and restore — the `FILES_IN` story resolves cleanly.** `/input` is a
   live, read-only host passthrough: a file written host-side after creation *and after the
   snapshot* is visible in the guest, still visible after `restore()`, and host-side edits
   read fresh. Guest writes into `/input` refuse with `PermissionError`. So per-call
   `write_file` for data files is implementable by writing to the host input dir — no
   re-snapshot, no content-signature cache. The counterpart surprise: **`restore()` deletes
   host-side `/output` files written after the snapshot.** The pull surface must collect
   before any restore; that ordering is a contract requirement, not a nicety.
2. **The micro-VM bar holds on (wasm × WHP), all four points.** Point 1 live:
   `winhvplatform.dll` loaded in the process actually running the sandbox, on two
   independent runs. Point 2 live: the guest's environment is empty, every host path probe
   refuses by absence (`FileNotFoundError`, never `PermissionError` — no host filesystem is
   exposed at all), and the metadata endpoint is unreachable even when explicitly
   allowlisted (connection refused — no route to link-local). Point 3 live: default is
   deny-all (`ErrorCode_HttpRequestDenied`), `allow_domain` produces a strict per-entry
   allowlist — a permitted sandbox still refuses unlisted hosts. Point 4 by inference: the
   only guest↔host channels observed are `call_tool`, `http_get`, `http_post` (injected
   globals, not importable modules) plus the constructor's two directories. This is the
   family table's first evidence row; (wasm × KVM) remains its own confirmation.
3. **Latency makes warm reuse the whole model; memory is the tight bound.** Constructor
   ~50–70 ms (lazy — it loads nothing); first `run()` ~4.2 s (the real cold start: module
   load plus guest heap); `snapshot()` ~0.65 s; `restore()`+run ~8.7 ms steady state; plain
   run ~0.4 ms; a 1 MB code string adds ~16 ms. The sobering number: a hard abort ("Guest
   aborted: 13 Out of physical memory") at **~40–50 MB of user allocation** under the 400Mi
   Windows default — CPython-in-wasm consumes most of the budget before user code runs.
4. **`run()` cannot be bounded from inside — a timeout needs a process boundary.** The
   native surface is seven methods; no interrupt, deadline, fuel, or cancellation exists at
   any layer. An infinite loop blocks the calling thread indefinitely (65 s observed, no
   return); only an OS-level kill of the process ends it, after which the host is healthy
   and a fresh sandbox works immediately. Failures split into two classes with different
   recoveries: **host-level aborts poison the sandbox permanently** (every later call fails
   instantly with "The sandbox was poisoned") — but **`restore(snapshot)` heals poison**;
   a hang is the one failure restore cannot reach, because the thread is stuck. So the
   adapter's shape is: snapshot after setup, restore to recover from aborts, kill-and-
   recreate the process for timeouts — and `run_code`'s timeout must exclude the one-time
   cold start. Two 0.4.0 platform bugs found on the way: `time.sleep` panics the runtime
   ("no Tokio reactor") and poisons the sandbox; `os.listdir` fails on every path
   (errno 44) — only direct `open()` works. The wider guest catalog for the instructions
   contract: no `threading`, no `subprocess`, no `os.fork`, no `socket`/`ssl`/`urllib`; the
   guest's execution namespace injects `call_tool`, `http_get`, `http_post`, `json`, `out`;
   `allow_domain` requires scheme-qualified targets at this version.
5. **The conformance suite applies nearly whole.** All 12 probe bodies in
   `maf_sandbox.conformance` call only `stat_file`/`read_file`/`list_dir` — none touch
   `exec` — so they run unchanged against this backend's pull surface. What breaks is one
   fixture: `PosixGuestSubject.plant_symlink` shells `ln -sfn` over `exec`, and `plant_layout`
   runs before any probe, so the shipped subject fails the whole suite at setup. The
   substitute is exactly what the module's own seam anticipated: a subject that plants
   files and links directly on the host side of `output_path()` — the code under test still
   has to walk the same real paths and discover the links itself. Six `run_code`
   conformance probes were drafted in the suite's own style (timeout honored; nonzero exit
   distinct from refusal; stdout/stderr separated; oversize code refused not truncated; no
   shell reachable; queue-time refusal distinguishable — the last blocked on #381's
   contract decision).
6. **No fallback exists: boot-or-refuse, verified aggressively.** No constructor kwarg, no
   env var (every plausible "disable hypervisor" name is inert — `winhvplatform.dll` stays
   loaded with all of them set), and no string in the compiled backend names an in-process
   or dry-run path. The only hypervisor-absence code path in the binary is the hard error
   "No hypervisor was found". The `HYPERLIGHT_*_SURROGATES` variables that do exist tune
   WHP's surrogate helper processes, not the boundary.
7. **The FFI callback's mechanics, exactly.** It fires synchronously **on the same OS
   thread that called `run()`** — no hidden worker. A host exception surfaces in-guest as a
   catchable `RuntimeError("Tool '<name>' failed: <type>: <msg>")` and the sandbox stays
   healthy; an unregistered name gets the same wrapper. Kwargs marshal with full fidelity
   (nested structures, non-ASCII intact); positional args refuse with a plain `TypeError`;
   unsupported return types arrive as `str()` — settled by the datetime case, str not repr.
   Registration after the first run refuses with an explicit message; a duplicate name
   silently last-wins. And the envelope is not a guess anymore: the shared output buffer is
   **16,376 bytes**, required = payload + **~192 bytes of framing** (constant across three
   failure sizes), so `framing_bytes` is a measured number — and exceeding the buffer is an
   in-guest-uncatchable abort that poisons the sandbox until a restore. Per-call round
   trips: ~0.07 ms at 10 B, ~0.3 ms at 15 KB. The trampoline's response ceiling must sit
   well below 16 KB, which also means the backend's declared host-tool `TransferLimits` are
   far below this package's defaults.

### What the measurements change in the design above

Three upgrades and one confirmation. The `FILES_IN` caveat in the fit table dissolves —
`write_file` is a plain host-side write. The `run_code` timeout obligation in #381 gains
its implementation shape: process-per-sandbox (or abandon-and-leak), with snapshot/restore
as the abort-recovery primitive — the thread-confined actor alone cannot bound a runaway.
The trampoline design gains hard numbers: a sub-16 KB response ceiling and a measured
~192-byte `framing_bytes`, plus the rule that oversize is a poisoning abort, not a clean
refusal, so the ceiling must be enforced host-side *before* the value crosses. And the
family table's `microvm` claim for (wasm × WHP) moves from "pending the bar" to "measured
against the bar" — with the memory headroom (~40–50 MB user allocation) recorded as the
number a workload's spec has to respect long before any transfer cap matters.

## Verdict

Yes — the package can back a backend, and the right dependency is `hyperlight-sandbox`
directly (pinned to an exact matched trio; the published ranges are demonstrably unsafe).
It slots in as the suite's first runtime-shaped backend: `microvm` isolation with
`ALLOWLIST` egress, serving `RUN_CODE` workloads, refusing `EXEC` ones honestly at attach.
Nothing found in the source contradicts #371's sequencing — the suite-side prerequisites
still come first — and with the probes run, the remaining unknowns are engineering choices
rather than feasibility questions: the bar is measured on (wasm × WHP), the pull pair and
per-call `write_file` are implementable against live host directories, the trampoline has
exact constants to build to, and the two failure classes have named recoveries (restore
for aborts, process kill for hangs).
