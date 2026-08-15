# 12 — when a sandbox goes away: within a turn, at its end, and on thread delete

Every other sample creates a sandbox and drops it on the way out, because every other sample is one program that runs once. A real host is not that. It serves many conversations, for a long time, and a sandbox is keyed by `(scope, thread_id, agent_dir)` — so it **outlives the turn that made it**, on purpose.

That leaves a host three moments where a sandbox can go away, and choosing between them is a cost decision rather than a style one.

**Not named for a backend, deliberately.** It runs on Docker because that is the cheapest place to watch containers appear and vanish, but the decision it argues about belongs to the backends where a sandbox costs money — ACAS above all. A `12_docker_…` name would put it behind the one prefix an ACAS reader filters out, and sample 11 already set the precedent of naming by subject when the subject is not one backend.

| Moment | Mechanism | What it is for |
|---|---|---|
| Within a turn | `acquire` is get-or-create | the second tool call does not pay for a second boot |
| End of turn | `async with router.scope(scope, thread)` | on a billable backend, the only sane default |
| Thread delete | `SandboxPurger.purge_scoped_thread` | the backstop, and the only thing that reclaims a conversation whose turns were never scoped |

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

## Run

```bash
cd samples/12_purge_lifecycle && uv run agent.py
```

Needs a Docker-compatible engine and nothing else — no cloud account, no model, no environment variables. It creates five containers over the run, one thread at a time, and reclaims all of them; the last thing it prints is how many were left behind.

## Where this sits

Sample 11 showed the router refusing. This one shows it reclaiming, which is the other half of what a host asks it for and the half with a number attached on a rented backend.
