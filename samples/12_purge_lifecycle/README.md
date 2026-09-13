# 12 — when a sandbox goes away: within a turn, at its end, on thread delete, and when it cannot

Every other sample creates a sandbox and drops it on the way out, because every other sample is one program that runs once. A real host is not that. It serves many conversations, for a long time, and a sandbox is keyed by the caller's scope, thread and agent identity — so it **outlives the turn that made it**, on purpose.

That leaves a host three moments where a sandbox can go away, and choosing between them is a cost decision rather than a style one.

**Not named for a backend, deliberately.** It runs on Docker because that is the cheapest place to watch containers appear and vanish, but the decision it argues about belongs to the backends where a sandbox costs money — ACAS above all. A `12_docker_…` name would put it behind the one prefix an ACAS reader filters out, and sample 11 already set the precedent of naming by subject when the subject is not one backend.

| Moment | Mechanism | What it is for |
|---|---|---|
| Within a turn | `acquire` is get-or-create | the second tool call does not pay for a second boot |
| End of turn | `async with router.scope(scope, thread)` | on a billable backend, the only sane default |
| Thread delete | `SandboxPurger.purge_scoped_thread` | the backstop, and the only thing that reclaims a conversation whose turns were never scoped |

There is a fourth moment, and it is the one a host does not choose: the framework tries to clean up after a call and **cannot**. Acts 5 to 8 force that for real and show what each of the host's three policies does about it, what the host is told, and what a handler that itself fails changes — see [below](#and-when-the-cleanup-cannot-run).

## The end-of-turn decision is where the money is

On Docker an idle container costs nothing, so keeping it between turns is free and the next turn starts warm. Copy that posture onto ACAS and it is a bill.

The two ACAS lifecycle bounds are **sequential, not one window**, and getting that wrong makes the argument sound bigger than it is. `AcasSandboxConfig` defaults to `auto_suspend_seconds=60` and `auto_delete_seconds=600`, and its own docstring describes the order: "suspension after idle, then deletion after being stopped."

| Elapsed since the last call | State |
|---|---|
| 0–60s | running and idle |
| 60s–~11min | suspended, and still resumable |
| after ~11min | deleted |

A suspended sandbox is not lost. `_backend.py` carries a 120-second resume timeout precisely so a slow resume is waited out rather than abandoned — its comment says abandoning one "pays a cold create instead — slower for the user and more expensive". So **warm reuse genuinely survives a gap of about eleven minutes**.

That makes the case for purging per turn narrower than it first looks, and it is still a case:

- **Under eleven minutes**, the sandbox is there and the next turn resumes it. Holding costs the idle minute before suspension.
- **Hours or days**, which is what a conversation actually looks like, outlives both timers. That idle minute is paid on every turn and the reuse it bought is gone before the next one arrives.

So `router.scope` on a billable backend spends nothing on idling and reclaims when the host decides rather than when the platform does. Warm reuse is real and worth having; for gaps of that size it is simply not on offer.

## Why the delete path still matters after that

Act 4 runs the purger against two conversations.

The first was purged at the end of its turn, so the purger finds **0**. That is the right answer, not a broken hook: a host that purges per turn should expect its delete path to find nothing almost every time.

The second ran with no `router.scope` around it — a host that never wired per-turn disposal, or a sandbox left by a worker that died before it could. The purger finds **1**, and nothing else would have.

Worth being precise about what that second case is *not*, because the tempting description is wrong: it is not an abandoned `async with`. That block disposes however it exits, exception included, so a scope once entered cannot orphan anything. The only way to reach a delete with work outstanding is never to have entered one.

What makes the delete path able to find it at all is that `dispose_scope` selects on the labels the backend stamped, not on anything the process remembers — so it reclaims sandboxes a replica never created, including a crashed worker's. After it there are only the platform's two timers, which reclaim on their own schedule rather than on the host's.

That asymmetry is the argument for wiring the purger even when you already purge per turn.

## Counted, not claimed

Every number here comes from `docker ps -a --filter label=maf-sandbox.thread=…`, the same labels `dispose_scope` selects on and the same `-a` the backend itself lists with when it purges. Without `-a` a container stopped but not removed would be invisible here while still sitting on the machine — the leak this sample exists to rule out, hidden from the check that rules it out. The library's return values say what it *believes* it disposed; the container count is what is actually on the machine, and only the second means anything for a leak. Where both appear, the sample prints them side by side so a disagreement would be visible.

The footer reports containers left behind, and the live check requires that number to be **0** — a sample about reclaiming sandboxes does not get to leak while saying so.

## Reuse is the same sandbox, not the same object

Act 1 proves reuse by writing a file through one `acquire` handle and reading it back through the next, rather than by comparing the two with `is`.

That distinction is load-bearing. The protocol promises "a running sandbox for `key`, creating one if needed" — the *sandbox*, not the object. `InProcessSandboxBackend` returns one object; `DockerSandboxBackend` returns a fresh `_DockerSandbox` handle over the same container. Both are correct, and an `is` check would fail against the second while claiming the backend was broken.

## And when the cleanup cannot run

Acts 1 to 4 are about a host choosing *when* a sandbox goes away. Acts 5 to 8 are about the case the host does not choose: the framework tries to clean up and **cannot**.

### The failure is real, and hardening is what causes it

Nothing is patched and no failure is injected. [`cleanup_probe.py`](cleanup_probe.py) runs a program that writes a file and then removes write permission from the directory holding it — `chmod 500`, three characters and the whole mechanism.

That only defeats a removal because of how the backend is configured. `DockerSandboxConfig(cap_drop_all=True)` runs the container with `--cap-drop ALL`, so its root holds no `CAP_DAC_OVERRIDE`, and without that capability root is bound by the same mode bits as anybody else. The backend's `reclaim` is `rm -rf`, tried as root and retried as the image's user where capabilities were dropped; here both are refused and both messages come back.

So the coupling is the lesson, not the trick: **hardening the container is what makes cleanup fallible.** Dropping capabilities is a real risk reduction that buys a real new failure mode, and the reporting path is how a host learns which calls paid for it.

The other precondition is the host's own. The per-call directory removal runs at `Cleanup.RECLAIM` and nowhere else, so the router sets `min_cleanup=Cleanup.RECLAIM`; a host that leaves the default floor disposes the sandbox after every call and never reaches this at all. The sample prints `router.effective_cleanup(spec)` rather than assuming it, and the live check refuses any other answer — at a different rung these acts would still print plausible numbers while demonstrating nothing.

### All three outcomes a host branches on

`ReclaimFailure.disposal` has three values and each is a different decision, so there is an act each.

| Act | What the host set | `disposal` | What followed |
|---|---|---|---|
| 5 | `DISPOSE` (the default) | `disposed` | **0** containers — the sandbox it could not clean is deleted |
| 6 | `KEEP` | `kept` | **1** container, still holding what would not go |
| 7 | a cleanup budget the engine cannot meet | `failed` | the next `acquire` on that key raises `SandboxUnclean` |

Act 5 is the guarantee: `acquire` is get-or-create, so a sandbox left warm would hand the next call in that conversation everything this one could not take back. Better a cold start than leaked data. Act 6 is the only setting that loosens it, and it loosens nothing else — `min_cleanup` is a separate floor, and a sandbox this router did not clean is still never adopted by a later process.

**Act 7 is the one whose reading is easy to get wrong.** `failed` means the framework could not *prove* the remedy landed — and in this run the container is gone, because `docker rm` was sent and the daemon finished it after the host had stopped waiting. So the refusal is not about a container that is still there. It is about a router that cannot say what state the key is in, refusing to serve the next call into it. `KEEP` does not loosen this one either: act 6's opt-down is about a reclaim, and this is the remedy for one. The sample prints that container count and the live check deliberately does not assert it, for the same reason.

A one-millisecond budget is not a setting anyone chooses; a budget too small for the engine underneath it is — a loaded daemon, a remote one, a cleanup competing with the next turn. It is set through `sandboxed_tool`'s per-tool `reclaim_timeout` rather than on the router, and that is not incidental: the router's value also bounds what it does *outside* a call, so starving it there means the router cannot clean an instance it has not served before, and the call is refused before its body runs with no cleanup left to fail. The two knobs are the pair [#520](https://github.com/sokolaidev/maf-extensions/issues/520) asks a sample to show — the router-wide default, and the per-tool override.

### A handler that raises is contained

Act 8 raises from the handler, and two things do not move: the call still answers what its body returned, and the record the handler wrote *before* raising is still there.

That ordering is the whole lesson. `sandboxed_tool` runs the handler inside an `except Exception` that logs and continues, so a handler that raises on its way to recording loses the record and leaves the run looking clean — every container count correct, and the reporting path quietly short one event. That is not hypothetical: it is the defect review found in sample 07's handler, which printed before it appended.

Both are read off `docker ps` and the exporter, like every other number here.

### The record the telemetry package cannot make

This is the first sample to wire [`maf-sandbox-otel`](../../packages/maf-sandbox-otel/), and acts 5 to 8 are where it earns its place: they show what it records, and then what it does not.

`OpenTelemetrySandboxObserver` is an observer on the *router*. It records what the library did. A reclaim that did not happen is reported to the **host** instead, through `ReclaimConfig(on_failure=...)`, because what to do about it is a host decision. So when a cleanup fails, here is what a collector receives without a handler:

- `sandbox.call` with `maf_sandbox.call.unclean = 0`. That attribute counts what a transport noted about processes it could not prove it stopped. A directory that would not go is not one of those.
- `sandbox.dispose` — **two** of them in acts 5 and 7, both `outcome=gone` in act 5, both carrying the same call id. One is the escalation; the other is the router cleaning an instance it had not served before, which happens inside `acquire`, ahead of the body, and on a perfectly healthy first call too. Nothing on either says which was which. Under `KEEP` there is one, and it is the adoption: the cleanup that did not happen leaves no record at all.

From the package's records alone, a call whose cleanup failed is indistinguishable from one that cleaned up perfectly. The three facts that separate them are the three `ReclaimFailure` carries and no event does: **which path**, **why**, and **what the framework did about it**.

[`telemetry.py`](telemetry.py) is the handler that writes them down, and it is the file to copy. Two decisions in it are worth reading:

**Where each attribute goes.** The join columns come from the package's public `Redaction` and its documented `maf_sandbox.call.id`, so the record groups with the call and the disposal it is about. The three new facts go under the host's own namespace — `app` here, your service's in yours — because `maf_sandbox.*` belongs to the package and a host writing new names into it would be squatting on a namespace a later version may define differently.

**It records before it does anything else.** `sandboxed_tool` runs the handler inside an `except Exception` that logs and continues, so a handler that raises on its way to the recording loses the record and leaves the run looking clean. Whatever else a host does — alerting, counting, paging — goes after that line.

The sample exports to an in-memory exporter so it can print what a collector would have received; a deployment swaps it for an OTLP one and changes nothing else. `record_sensitive_data=True` is set here because everything this sample names is its own — its scope, its thread ids, a container it created. That is a deployment's decision, and with it off the reason and the path are withheld while the disposal outcome and the join columns still come out.

## Run

```bash
cd samples/12_purge_lifecycle && uv run agent.py
```

Needs a Docker-compatible engine and nothing else — no cloud account, no model, no environment variables. It creates nine containers over the run, one thread at a time, and reclaims all of them; the last thing it prints is how many were left behind, beside how many reclaim failures were recorded.

That second number is the one to watch, and it fails in the opposite direction from everything else here: the check requires it to be **4**, one per act from 5 to 8. A handler nothing ever calls reports nought, and a check reading nought agrees with it — which is exactly how a bug in the reporting path ships unnoticed ([#760](https://github.com/sokolaidev/maf-extensions/issues/760)).

## Where this sits

Sample 11 showed the router refusing. This one shows it reclaiming, which is the other half of what a host asks it for and the half with a number attached on a rented backend — and then what happens when the reclaiming fails, which is the half no other sample reaches at all.

Sample 07 wires the same `ReclaimConfig` on a healthy workload, where the count is nought every time and the live check requires it to be. The two are the pair: one shows the policy a host sets, the other shows it firing.
