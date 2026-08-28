# `wslc` — WSL containers

> The developer-machine backend on Windows: a container in about half a second, no daemon and no login, serving `exec` and files *in* only. Install and configuration: [`packages/maf-sandbox-wslc/README.md`](../../../packages/maf-sandbox-wslc/README.md).

## What it declares

The four below `isolation` are fields of this backend's `declarations`.

| Declaration | Value |
|---|---|
| `isolation` | `Isolation.CONTAINER` |
| `capabilities` | `EXEC`, `FILES_IN` — the narrowest set any shipped backend declares, and it has not grown: no `FILES_OUT`, no `FILES_LIST`, no `FILES_DELETE`, no `RUN_CODE` |
| `egress_modes` | `{Egress.CLOSED}`; `{Egress.CLOSED, Egress.ALLOWLIST}` when an egress proxy image is configured. Never `UNRESTRICTED` |
| `limits` | **not declared** |
| `os_families` | **not declared**. Its guests are WSL containers and nothing states the family, so the router reads `frozenset()` and refuses a spec that asks |

`container` is below the router's default floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER`; with nothing passed, construction raises. That refusal is the point of the declaration, not a limitation to work around — there is no flag left to forget.

## What it needs

**Windows with WSL 2.9.3 or later**, and nothing else to install: `wslc` ships as part of WSL. Every call spawns `wslc.exe`, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so. This is the one shipped backend that runs on a single operating system, which is exactly why [`docker`](docker.md) rather than this one carries the live gate.

## Lifecycle

Creates land in **about half a second**, which is what makes this the backend to iterate against. Names are derived from a digest of scope, thread, agent dir, kind and egress identity, so acquire and dispose agree without a registry; get-or-create is serialised per `(loop, key, kind)`, because a create names no container until it returns and two racing acquires would each build a network, a proxy and a sandbox. Labels are written at create and `dispose_scope` selects on them from the CLI's own listing, with values hashed rather than truncated for the reason every backend here hashes them — a shared prefix would let one conversation's purge delete another's containers.

**A spec's mode is enforced or refused, never approximated.** With no proxy image the set is `{CLOSED}` alone, so a workload running `ALLOWLIST` is refused at attach rather than handed the closed run it did not ask for; with one, both modes are enforceable and a spec naming no hosts still resolves to the closed shape. What the modes mean is [`../network.md`](../network.md).

**Egress scaffolding is re-ensured on every acquire, not only on create.** A proxy a host reboot stopped, or one a crashed setup left half-connected, is rebuilt here — the alternative is handing back a sandbox that declares an allowlist and enforces nothing, which is the exact failure the honesty rule exists to prevent. The topology is the internal-network-plus-CONNECT-proxy shape [`docker`](docker.md) copies verbatim; the axis is [`../network.md`](../network.md).

## Why it serves neither `FILES_OUT` nor `FILES_LIST`

`wslc container cp` has three forms — local→container, container→local, and stdin→container — and **no container→stdout form**, so there is no reverse of the one-entry tar it writes on the way in and no tar header to read a type and size from before the content. Container→local writes a raw file to a host path with no header at all. Worse, a **symlink source exits 0, writes nothing to stderr, and produces a 0-byte file** — neither preserved, nor followed, nor refused, and indistinguishable from a legitimately empty artifact. Confinement requires refusing a link whether or not its target would have resolved somewhere legitimate, and this mechanism cannot tell the difference at all. Serving the capability anyway would mean an `exec`-based `stat` before every read, which requires the image to contain a shell — precisely the dependency the `FILES_LIST` split exists to avoid. So `stat_file`, `read_file` and `list_dir` raise `NotImplementedError` naming the backend and the reason: the router refuses such a spec before a workload runs, and the raise is the honest floor under a caller that skipped the check, where a bare `AttributeError` would name neither the backend nor the file and read as unrelated to a `write_file` that had just succeeded.

Deferred rather than rejected, in [#125](https://github.com/sokolaidev/maf-extensions/issues/125), and filed upstream as [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (the symlink bug) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (the missing stdout form) — **either of which reopens the question**.

## No `FILES_DELETE`, no `RUN_CODE`, and no `limits`

`remove` raises too, and not for want of `rm`: confining a removal means walking the path's parents, and `stat_file` is the walk this backend has none of. The gap is the same one, tracked in the same place.

`run_code` raises for a different reason, and one this backend shares with [`acas`](acas.md) and [`docker`](docker.md): it is a `Sandbox` method, so it is implemented, but *which* runtime an image carries is a property of the image and this backend does not parse the reference it is handed. Declaring `RUN_CODE` would be a claim about someone else's artefact. A workload wanting an interpreter by name execs it and owns that assumption.

**`limits` is not declared at all**, and that silence is read the way a safety claim's silence is read — as the conservative default, `DEFAULT_SANDBOX_LIMITS`, rather than as "no ceiling". The router refuses a spec asking above it. Since this backend serves no out-door, the direction that matters is `files_in`. See [`../capabilities.md`](../capabilities.md).

## `reclaim` is served, and the refusal above is the reason it can be

`reclaim` is mandatory and gated by no capability, and this backend serves it — `rm -rf` over the same `wslc container exec` invocation `exec` itself uses, run directly as `rm` rather than through the in-image shell `EXEC` names. That does not soften the `remove` refusal above, and it is not the delete surface arriving by a side door. `remove` takes a path a model named, so it owes the parent walk, and the walk is what this backend has no `stat_file` to build. `reclaim` takes a directory the framework created under `working_directory` with an unguessable name, so there is no attacker-chosen component to walk and no confinement to owe. The `rm` runs as `--user 0`, not as the image's user: the file plane (`write_file`) writes as root, so on a non-root image the guest cannot remove what a call left behind, and reclaim raises authority to take the framework's own call directory back. Because it runs as root, the two refusals docker's and acas's reclaims carry — a path that is not absolute, and one fewer than two components from the root — stand here too, before any `exec` reaches the engine; confinement is not owed, but a recursive irreversible delete that carries root's authority refuses what it cannot place. The one duty this backend cannot discharge is the whole of why it refuses `remove`, and it is exactly the duty `reclaim` does not carry — which is why the narrowest capability set any shipped backend declares costs a kind nothing here: a call's directory is reclaimed on this backend the way it is on every other. `-f` is what makes an already-gone directory a success, which is the contract's rule rather than a convenience; anything else raises, and the caller in a `finally` turns that into a report.

## Status

| Decision | State | Tracking |
|---|---|---|
| The backend, `EXEC` and `FILES_IN`, both egress modes, label purge | shipped | — |
| `egress_modes = {CLOSED}`, or `{CLOSED, ALLOWLIST}` with a proxy image; a mode outside the set is refused rather than degraded | shipped | [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| `run_code` implemented as a refusal, `RUN_CODE` undeclared | shipped — the capability set is still `{EXEC, FILES_IN}` | [#531](https://github.com/sokolaidev/maf-extensions/pull/531) (merged) |
| wslc serves `FILES_OUT` | deferred — `cp` has no container-to-stdout form, and a symlink source writes a 0-byte file at exit 0 | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open; upstream [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310), both open |
| wslc serves `FILES_LIST` | deferred — would need an in-image shell, the dependency the split exists to avoid | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open |
| wslc declares `FILES_DELETE` | open — confining a removal needs the component walk `stat_file` would provide | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open |
| `reclaim` served over `exec`, while the `remove` refusal stands unchanged | shipped — the backend the mandatory-and-un-gated shape was argued from, since a directory the framework created owes none of the confinement `remove` owes; a test pins the refusal beside the served member. The two placement refusals the other reclaims carry — not absolute, too close to the root — landed here as well ([#711](https://github.com/sokolaidev/maf-extensions/issues/711)), since this is the reclaim that runs as root | [#477](https://github.com/sokolaidev/maf-extensions/issues/477), [#711](https://github.com/sokolaidev/maf-extensions/issues/711) |
| No `limits` declaration; silence resolves to `DEFAULT_SANDBOX_LIMITS` | shipped — deliberate, and the conservative direction | — |
| Shared egress probes — this topology answers the same egress contract the other backends answer | shipped, with the CI half unchanged: `test_wslc_e2e.py` calls `assert_egress_conformance` for the outcome and keeps its own stricter `000` check. Where it runs has not moved — the whole suite needs Windows with a WSL that ships `wslc`, which no offered runner is, so it runs on a developer's machine and never in CI, and [`docker`](docker.md)'s identical leg on every pull request is what actually guards the topology the two copy byte for byte | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) (open) — the probe [#547](https://github.com/sokolaidev/maf-extensions/pull/547), the wiring [#548](https://github.com/sokolaidev/maf-extensions/pull/548) (merged) |
| A guest-platform axis a kind can declare and match | shipped in core, unanswered here — `os_families` exists and this backend declares none, so a spec asking for a family is refused rather than served | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged); the declaration itself is [#588](https://github.com/sokolaidev/maf-extensions/issues/588) (open) |
| Which principal a file-plane call acts as | measured live, then fixed. The file plane (`write_file`, a tar into `wslc container cp`) writes as **root (0:0)** regardless of the image's `USER`; `exec` with no `--user` runs as **the image's `USER`**; `reclaim` was `rm -rf` over that `exec`, so on a non-root image the guest could not remove what the file plane wrote — `rm` exited 1 ("Permission denied") and the call directory leaked, a visible failure (reclaim raised `OSError`), not a silent one. This raises `reclaim` to `--user 0` so the framework's own call directory is taken back as root. wslc does not `--cap-drop`, so root reclaim always succeeds and no retry-as-guest fallback is owed (the shape [`docker`](docker.md) needed before [#684](https://github.com/sokolaidev/maf-extensions/pull/684), and does not need here). `remove` is still not served, so `reclaim` remains the only member with the split. Every image this repository otherwise runs is root's, which is what kept it invisible. The same split measured on [`acas`](acas.md) closed the other way — its data plane already acts as the host | [#695](https://github.com/sokolaidev/maf-extensions/issues/695) (closed) — here by [#706](https://github.com/sokolaidev/maf-extensions/pull/706) (merged), on acas by [#707](https://github.com/sokolaidev/maf-extensions/pull/707) (merged) |
