# `wslc` — WSL containers

> The developer-machine backend on Windows: a container in about half a second, no daemon and no login, serving `exec` and files *in* only. Install and configuration: [`packages/maf-sandbox-wslc/README.md`](../../../packages/maf-sandbox-wslc/README.md).

## What it declares

| Declaration | Value |
|---|---|
| `isolation` | `Isolation.CONTAINER` |
| `capabilities` | `EXEC`, `FILES_IN` |
| `egress` | `Egress.CLOSED`; `Egress.ALLOWLIST` when an egress proxy image is configured |
| `limits` | **not declared** |

`container` is below the router's default floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER`; with nothing passed, construction raises. That refusal is the point of the declaration, not a limitation to work around — there is no flag left to forget.

## What it needs

**Windows with WSL 2.9.3 or later**, and nothing else to install: `wslc` ships as part of WSL. Every call spawns `wslc.exe`, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so. This is the one shipped backend that runs on a single operating system, which is exactly why [`docker`](docker.md) rather than this one carries the live gate.

## Lifecycle

Creates land in **about half a second**, which is what makes this the backend to iterate against. Names are derived from a digest of scope, thread, agent dir, kind and egress identity, so acquire and dispose agree without a registry; get-or-create is serialised per `(loop, key, kind)`, because a create names no container until it returns and two racing acquires would each build a network, a proxy and a sandbox. Labels are written at create and `dispose_scope` selects on them from the CLI's own listing, with values hashed rather than truncated for the reason every backend here hashes them — a shared prefix would let one conversation's purge delete another's containers.

**Egress scaffolding is re-ensured on every acquire, not only on create.** A proxy a host reboot stopped, or one a crashed setup left half-connected, is rebuilt here — the alternative is handing back a sandbox that declares an allowlist and enforces nothing, which is the exact failure the honesty rule exists to prevent. The topology is the internal-network-plus-CONNECT-proxy shape [`docker`](docker.md) copies verbatim; the axis is [`../network.md`](../network.md).

## Why it serves neither `FILES_OUT` nor `FILES_LIST`

`wslc container cp` has three forms — local→container, container→local, and stdin→container — and **no container→stdout form**, so there is no reverse of the one-entry tar it writes on the way in and no tar header to read a type and size from before the content. Container→local writes a raw file to a host path with no header at all. Worse, a **symlink source exits 0, writes nothing to stderr, and produces a 0-byte file** — neither preserved, nor followed, nor refused, and indistinguishable from a legitimately empty artifact. Confinement requires refusing a link whether or not its target would have resolved somewhere legitimate, and this mechanism cannot tell the difference at all. Serving the capability anyway would mean an `exec`-based `stat` before every read, which requires the image to contain a shell — precisely the dependency the `FILES_LIST` split exists to avoid. So `stat_file`, `read_file` and `list_dir` raise `NotImplementedError` naming the backend and the reason: the router refuses such a spec before a workload runs, and the raise is the honest floor under a caller that skipped the check, where a bare `AttributeError` would name neither the backend nor the file and read as unrelated to a `write_file` that had just succeeded.

Deferred rather than rejected, in [#125](https://github.com/sokolaidev/maf-extensions/issues/125), and filed upstream as [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (the symlink bug) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (the missing stdout form) — **either of which reopens the question**.

## No `FILES_DELETE`, and no `limits`

`remove` raises too, and not for want of `rm`: confining a removal means walking the path's parents, and `stat_file` is the walk this backend has none of. The gap is the same one, tracked in the same place.

**`limits` is not declared at all**, and that silence is read the way a safety claim's silence is read — as the conservative default, `DEFAULT_SANDBOX_LIMITS`, rather than as "no ceiling". The router refuses a spec asking above it. Since this backend serves no out-door, the direction that matters is `files_in`. See [`../capabilities.md`](../capabilities.md).

## Status

| Decision | State | Tracking |
|---|---|---|
| The backend, `EXEC` and `FILES_IN`, both egress modes, label purge | shipped | — |
| wslc serves `FILES_OUT` | deferred — `cp` has no container-to-stdout form, and a symlink source writes a 0-byte file at exit 0 | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open; upstream [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310), both open |
| wslc serves `FILES_LIST` | deferred — would need an in-image shell, the dependency the split exists to avoid | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open |
| wslc declares `FILES_DELETE` | open — confining a removal needs the component walk `stat_file` would provide | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open |
| No `limits` declaration; silence resolves to `DEFAULT_SANDBOX_LIMITS` | shipped — deliberate, and the conservative direction | untracked |
| Shared egress probes — this topology's allowlist has never been measured against a live host in CI | open | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) open |
