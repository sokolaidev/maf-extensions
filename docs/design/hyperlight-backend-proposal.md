# `maf-sandbox-hyperlight`: the design for a Hyperlight micro-VM backend

> **Status: PROPOSED — no package exists.** This is the design record #371 asked for: it
> fixes the layer boundary, states what `RUN_CODE` has to mean, and answers the six
> blockers that issue named. The analysis it rests on — the source read of the Hyperlight
> stack, the fit against the protocol, and the seven live probes with their measured
> constants — is [`../research/hyperlight-backend-exploration.md`](../research/hyperlight-backend-exploration.md),
> and this document does not re-argue any of it. The vocabulary is
> [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md)'s; the transport question it
> defers is [#369](https://github.com/sokolaidev/maf-extensions/issues/369)'s; the one
> decision it deliberately leaves open is
> [#425](https://github.com/sokolaidev/maf-extensions/issues/425)'s.

We propose a seventh package, `maf-sandbox-hyperlight`, that implements
`maf_sandbox.SandboxBackend` over the [`hyperlight-sandbox`](https://github.com/hyperlight-dev/hyperlight-sandbox)
Python stack — a hardware micro-VM (KVM, WHP or MSHV) running a Wasm guest, with a CPython
interpreter compiled into it. It is the suite's first *runtime-shaped* backend: it declares
`Isolation.MICROVM` and serves `RUN_CODE`, and it **raises from `exec`** — no shell, no
argv, no processes exist in its guest. The attach gate makes that honest: a spec requiring
`EXEC` (including every say-nothing spec, since `DEFAULT_CAPABILITIES` carries it) is
refused before anything boots, which is the capability match doing its job.

**The layer boundary, fixed.** The dependency is `hyperlight-sandbox` directly — not
`agent-framework-hyperlight`, whose CodeAct provider and `execute_code` tool are the layers
this suite already ships. The composition rule is normative: the other stack's layers
become a backend *beneath* this suite's contract, never an alternative beside it — a host
that attaches `HyperlightCodeActProvider` next to this router has bypassed every refusal
the router makes, and no code can prevent that; this sentence is the mitigation.

**Pinning is exact, not a range.** The published wheels are demonstrably skewed: the guest
package's AOT artifact embeds a Wasmtime version the backend wheel refuses, `hyperlight-sandbox`
pins the guest with no compatible upper bound, and the native binding cannot JIT raw wasm —
so a naive install produces a stack that cannot execute at all. The package pins the exact
matched trio it was conformance-tested against and treats a bump as a new conformance run.

## Names

| Thing | Value | Convention it follows |
|---|---|---|
| Distribution | `maf-sandbox-hyperlight` | `maf-sandbox-docker`, `maf-sandbox-acas` |
| Import package | `maf_sandbox_hyperlight` | `maf_sandbox_docker` |
| `SandboxBackend.name` | `"hyperlight"` | the value `selected=` matches on |
| Backend class | `HyperlightSandboxBackend` | `DockerSandboxBackend` |
| Config class | `HyperlightSandboxConfig` | `DockerSandboxConfig` |
| Warning category | `MafSandboxHyperlightExperimentalWarning` | per-package `UserWarning` subclass |

The tag glob is distinct (`maf-sandbox-h*` collides with nothing), per `docs/maintainers.md`'s
standing rule.

## What the backend declares

**The backend is a family, and every declaration derives from the configured guest.** The
constructor takes the inner backend choice (`"wasm"` today; `"hyperlight-js"` is defined
upstream and unshipped) and resolves every declaration from a table keyed by it, refusing a
choice with no entry — mirroring the stack's own refuse-unknown. Resolved at construction,
static afterward, which is what the router's construction-time floor check assumes. A
custom guest module (`module_path`) is **a different family member starting with no
declarations** — refused, or admitted through its own conformance pass. No config field may
change what `isolation` returns; the family table changes which *entry* was constructed.

For the one shipped entry, (wasm guest × WHP), measured 2026-08-18:

**`isolation` returns `Isolation.MICROVM`.** The micro-VM standard's four points held live:
`winhvplatform.dll` loaded in the executing process; no host filesystem exposed at all
(refusal by absence, never permission); the metadata endpoint unreachable even when
explicitly allowlisted; egress default-deny with a strict per-entry allowlist; the only
guest↔host channels are the declared ones. No weaker-than-hypervisor mode is reachable —
no kwarg, no env var, no code path in the binary; the one hypervisor-absence path is the
hard error "No hypervisor was found". (wasm × KVM) claims the rung only after its own
conformance run.

**`capabilities` returns `frozenset({RUN_CODE, FILES_IN, FILES_OUT, FILES_LIST, SNAPSHOT})`.**

- **`RUN_CODE`** — the method this document specifies below.
- **`FILES_IN`** — `write_file` is a plain host-side write: the guest's `/input` is a
  live, read-only passthrough of a host directory, and a file written after creation *and
  after the snapshot* is guest-visible and survives `restore()`. No re-snapshot, no
  content-signature cache.
- **`FILES_OUT` and `FILES_LIST`** — the pull pair and enumeration, served against the
  *host* side of the output directory with `refuse_symlinked_parents` walking real paths.
  Unlike Docker, enumeration costs nothing here — the directory is local — so `FILES_LIST`
  is declared from day one. One rule is contract, not implementation detail:
  **collection happens before any restore**, because `restore()` deletes host-side output
  files written after the snapshot.
- **`SNAPSHOT`** — real and load-bearing: it is both the warm-reuse mechanism (~8.7 ms
  restore+run against a ~4.2 s cold start) and the abort-recovery primitive (below).
- **Not `EXEC`.** Raises, per the established raise-from-ungated-method pattern; the
  attach gate refuses first. `os.fork` and the `subprocess` module do not exist in the
  guest — the omission is by construction, not by policy.
- **Not `HOST_TOOLS`, yet.** The channel exists and is measured (the `call_tool` FFI), but
  what the capability *means* is #369's question and that decision is held until #302's
  measurement, per the sequencing recorded there. The contingent design is below so it
  lands by declaration flip, not redesign.
- **Not `NETWORK`.** Same deliberate omission as the Docker proposal's, for the same
  reason: no shipped backend declares it and its meaning is still a mapping exercise. The
  egress axis carries the confinement claim.

**`egress` returns `Egress.ALLOWLIST`, unconditionally.** Enforcement is native, at the
guest's wasi-http boundary, per target — verified live: default deny with a precise error,
a permitted sandbox still refuses unlisted hosts. Method-scoped entries are #377, additive;
the adapter ships host-wide entries and adopts the refinement when the axis lands. One
mapping note the implementing PR inherits: the stack requires scheme-qualified targets, so
the adapter registers a spec's bare hostnames under both schemes — the widening is from
one hostname to its two schemes, never to a different host.

**`limits` declares the transfer ceilings**, with one number that is not ours to choose:
whenever host tools arrive, the per-dispatch response ceiling must sit well below the
measured **16,376-byte** shared-buffer capacity, because exceeding it is not a refusal —
it is an abort that poisons the sandbox. Transfer limits for files are policy constants as
in the Docker proposal; the memory reality (~40–50 MB of user allocation under the default
heap before a hard abort) is recorded as workload guidance, not a declared axis — the
resource-ceiling non-axis is the parity ledger's item 4, kept deliberately.

## What `RUN_CODE` means — the `run_code` contract (#381)

`RUN_CODE` gates a method, exactly as `FILES_OUT` gates the pull pair:

```python
async def run_code(self, code: str, *, timeout: float) -> ExecResult
```

`ExecResult` already fits — stdout, stderr, exit code; the stack's own result is the same
shape. The measured semantics this backend gives it:

- **Failure taxonomy, two classes, two recoveries.** Ordinary guest failures (`sys.exit`,
  an unhandled Python exception) return a normal `ExecResult` with a nonzero exit code and
  the sandbox stays reusable. Host-level aborts (memory exhaustion, runtime panics,
  oversized FFI payloads) poison the sandbox permanently — and **`restore(snapshot)` heals
  poison**, so the adapter snapshots after setup and restores to recover, at ~0.65 s for
  the snapshot and milliseconds for the restore.
- **A hang is the one failure restore cannot reach.** No timeout, interrupt or fuel exists
  anywhere in the stack; an unbounded loop blocks the calling thread indefinitely and only
  an OS-level process kill ends it (after which the host is healthy and a fresh sandbox
  works immediately). So `timeout` is enforced by kill: the sandbox lives where a kill can
  reach it, and the implementing pull request settles the process topology — per-sandbox or
  per-run — with the ~4.2 s cold start in view. Abandon-and-leak (a stuck thread holding a
  400Mi guest forever) is rejected.
- **The bound excludes the cold start.** First `run()` pays the one-time module load;
  `timeout` measures the guest program, not the boot.
- **Queue time is distinguishable.** One sandbox serves one call at a time; a call that
  exhausts its budget waiting behind another is refused distinctly from a program
  overrunning, because the model's correct next action differs. The exception taxonomy is
  #381's to finalize.
- **The guest execution contract, stated in the kind's instructions.** Output is stdout
  only — the last expression's value does not return; end with `print(...)`. Artifacts go
  to `/output`. The quirk catalog the instructions must respect at the pinned version: no
  `os.listdir` (open by exact path), no `time.sleep` (a runtime panic that poisons), no
  `threading`, no `socket`/`ssl`/`urllib` (HTTP is the injected `http_get`/`http_post`,
  present only when egress is allowed), and the ~40–50 MB allocation ceiling.

`work_dir` maps to the guest's one writable tree: the adapter accepts `/output` (and paths
under it) and resolves the pull surface against the host side of that directory. A spec
stating any other `work_dir` is refused at acquire — the honest answer for a guest whose
filesystem is two mapped directories and a synthetic module namespace.

## Host tools, contingent on #369 — recorded so the flip is small

When `HOST_TOOLS` is declared, the channel is the backend's own: the `call_tool` builtin
ships inside the guest, so the guarantee-by-construction reading applies with no probe
needed. The trampoline rule is absolute — the adapter registers one trampoline per sealed
registry name, and the trampoline's body is a call to the current run's
`HostToolRun.dispatch`; the kind's callables are never registered directly, because the
wire's entire native dispatch policy is a name lookup. The measured facts the trampoline
builds to: the callback fires synchronously on the OS thread that called `run()`; a host
exception surfaces in-guest as a catchable `RuntimeError` with the sandbox staying healthy;
`framing_bytes` is ~192; the response ceiling is enforced host-side *before* the value
crosses, because oversize is a poisoning abort, not a refusal. Registration is
before-first-run only, so the registry's sealed name set joins the sandbox's cache
identity.

## The six blockers of #371, answered

| # | Blocker as filed | Where it landed |
|---|---|---|
| 1 | `RUN_CODE` gates nothing | #381 — the method above |
| 2 | codeact hard-requires `EXEC` | #425 — both shapes recorded there, decision deferred there deliberately |
| 3 | The host-tool transport is not pluggable | #369, shape C argued, held until #302 by its own sequencing; the contingent section above |
| 4 | `FILES_OUT` push-vs-pull mismatch | Dissolved by measurement: the pull pair reads a real host directory; pull-before-restore is the contract |
| 5 | Guest-platform assumptions | Dissolved on this path: the program is text, not a file — no interpreter sentence exists to go false. #111 stands for `EXEC` backends |
| 6 | The router picks one backend at construction | #328/#329, real the day this backend registers beside a shell backend |

## Conformance

The suite's twelve pull-surface probes apply unchanged — none touches `exec`. What breaks
is one fixture: the shipped subject plants symlinks by shelling `ln` over `exec`, which
this backend refuses at the first plant. The package ships its own `ConformanceSubject`
that plants files and links directly on the host side of the output directory — the seam
the conformance module explicitly reserved for exactly this — and the code under test
still walks the same real paths and discovers the links itself. Six `run_code` probes are
drafted in the research note in the suite's own style (timeout honored; nonzero exit
distinct from refusal; stdout/stderr separated; oversize code refused, never truncated; no
shell reachable; queue-time refusal distinguishable — the last blocked on #381's
taxonomy); they land with #381, and the `microvm` claim is re-earned per family entry.

## Sequencing

#381 first — it is held on nothing and this package is unbuildable without it. The package
ships without host tools, serving `RUN_CODE` workloads (#425 decides how codeact becomes
one); `HOST_TOOLS` flips on when #302 → #369 resolve; per-spec routing (#328/#329) becomes
urgent the day a host registers this backend beside a shell backend. Tracking: #382.
