# `docker` — plain containers

> Container-rung sandboxes on any Docker-compatible engine — the backend a contributor can run on the machine in front of them, and the one whose live suite is the repository's acceptance gate. Install and configuration: [`packages/maf-sandbox-docker/README.md`](../../../packages/maf-sandbox-docker/README.md).

## What "Docker" means here

A `docker`-compatible command-line client talking to a Docker-API-compatible socket — not Docker Inc.'s daemon specifically, and not any particular installation shape; the full argument for targeting the contract rather than the binary is [`../research/docker-backend-exploration.md`](../research/docker-backend-exploration.md).

## What it declares

| Declaration | Value |
|---|---|
| `isolation` | `Isolation.CONTAINER` |
| `capabilities` | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_DELETE`, `HOST_TOOLS` |
| `egress` | `Egress.CLOSED`; `Egress.ALLOWLIST` when an egress proxy image is configured |
| `limits` | 64 MiB per file, 256 MiB total, 256 files — the same `TransferLimits` in each direction |

## `container` is a constant

`isolation` is not a function of the config, in this version or any later one. A container shares the host kernel and no setting this package has can change that; a Docker Desktop or Colima VM does not lift the rung, because one shared VM kernel serves every container — the same shape as wslc's WSL 2 utility VM, which the ladder also classifies at `container`. A hardened runtime would be a different rung, but only with a way to *verify* it is in effect, and a runtime string a backend cannot verify must never become a security guarantee the router repeats. The rung sits below the router's default floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER`; with nothing passed, construction raises. Three self-imposed constraints hold the guest↔host surface narrow — no bind mounts, no host-path sharing, and never `/var/run/docker.sock` passthrough — and they are self-imposed: nothing checks that a backend declaring `container` refrained from mounting the host filesystem.

## The pull surface: one tar, read twice

Stat and read are the **same `docker cp <name>:<path> -` tar stream**, which works against an image with no shell at all — the property that makes a file surface possible on a minimal image. `stat_file` reads only the **first 512-byte tar header**, which carries the size, the entry-type flag and the link target, and kills the transfer: nothing after the header moves, so an output too large to serve costs one block rather than its whole self. `read_file` re-runs the same copy bounded to header plus `max_bytes`, so a file over the cap is **refused on its header without its body ever being buffered**, and the child is killed and reaped rather than drained. A non-regular entry is refused twice independently — on the stat's entry kind, and again on the tar entry's type bit, since `docker cp` without `-L` tars a symlink as a link *entry* rather than the target's bytes. A hard link stays `EntryKind.OTHER`: it names an inode rather than a path, so it is no way out of the working directory, and it is refused as non-regular regardless. Every parent is classified first, from the filesystem root down, through the shared `refuse_symlinked_parents` walk over this backend's own unconfined stat — one header read per component. The residual that walk cannot close is stated rather than papered over: a guest that turns a stat-ed component into a link between the walk and the read wins, since `docker cp` has no no-follow form.

The tar header is the settled mechanism by maintainer ruling, in preference to `HEAD /containers/{id}/archive` and its `X-Docker-Container-Path-Stat`: the header carries the same daemon-provided answer one layer down, and it keeps the driver at zero dependencies where the HTTP endpoint would reintroduce exactly the socket and context handling the CLI-driver choice exists to avoid. The one field the header lacks is modification time, and nothing needs it yet.

## No `FILES_LIST`, and that gap is why the split exists

Docker has **no engine-level primitive for enumerating a directory**. A *named* path can be statted and copied against an image with no shell; discovering what names exist would need either an in-container `ls`/`find`, which depends on the image having one while the copy path does not, or tarring a whole directory to read its entry headers, which transfers every byte a size cap exists to refuse. That is the backend the capability split was named for — *name the backend that lacks it; if none does, it is a comment, not a capability* — so a kind requiring `FILES_LIST` is refused here, which is the match doing its job rather than a defect. `list_dir` raises `NotImplementedError` with that reason: the router refuses such a spec before a workload runs, so the raise is the honest floor under a caller that skipped the check. See [`../capabilities.md`](../capabilities.md).

## `FILES_DELETE` is declared, over `rm`

`remove` runs `rm -f` — or `rm -rf` when `recursive` — after confining the path and walking its parents, because the engine has no delete primitive. `rm`'s exit codes are the contract rather than a re-implementation of it: `-f` makes a missing path succeed and refuses a directory without `-r`. The image dependency is the one the `EXEC` declaration already names, so nothing new is being assumed. The working directory itself is refused before the subprocess is built.

## `HOST_TOOLS` on a live measurement

As on ACAS, `HOST_TOOLS` has no method behind it and asserts that **`exec` detaches** — a process started by one call outlives it and is observable from the next, because the container *is* the sandbox and it stays up between calls. `test_docker_e2e.py` measures it rather than assuming it. It is not a claim about the image.

## Egress topology

Closed mode is `--network none`: a network namespace with nothing but loopback, enforced by whichever kernel runs the container. Allowlist mode is an **internal network plus a dual-homed CONNECT proxy** — an `--internal` network so nothing on it has a route off it, a proxy container on that network carrying the spec's allowlist, its outbound leg connected to a second network, and the workload created with `HTTP_PROXY`/`HTTPS_PROXY` pointing at it. The proxy is recreated on every acquire rather than adopted, so a stale or half-connected one is never mistaken for a working one, and a failure to connect the outbound leg fails the acquire — a proxy without it would turn `ALLOWLIST` into `CLOSED` silently. `egress` states what the backend *can enforce*, not what one sandbox got: a backend configured with a proxy image still creates a `--network none` container for a spec that allows nothing, because denying everything for free beats burning a network and a proxy on allowing the same nothing.

What enforces the allowlist is the **topology**, not the environment variables: `HTTP_PROXY` and `HTTPS_PROXY` are advisory conventions a binary can ignore, but the workload sits on a network with no other route off it, so the proxy is not the polite way out — it is the only way out. No TLS is decrypted and no name is resolved inside the sandbox. Host-level `iptables` allowlisting is deliberately not part of the contract and cannot be made so: under rootless Docker the rules land in the wrong namespace, and on macOS and Windows the daemon lives in a VM the host firewall cannot address. The live controls that ship are a CONNECT pair against a real engine — an allowed host reachable, a denied one refused; the engine's embedded DNS resolver forwards external lookups from the daemon, outside the container's namespace, and whether it still does so on a network created `--internal` is engine behaviour this repository has identified rather than closed. The proxy source is a **byte copy** of wslc's, pinned identical by a test rather than hoisted into core — the maintainer ruling that keeps operational content out of a stdlib-only package. The axis is [`../network.md`](../network.md).

## Lifecycle

Names are **derived** from a digest of scope, thread, agent dir, kind and egress identity, so acquire and dispose agree without a registry; the kind is in the identity because a sandbox carries its spec's image and egress policy and must never serve two kinds, and the egress identity is in it because a sandbox is only reusable by an acquire that wants the same egress. Every acquire takes a per-`(loop, key, kind)` lock and lands in one of four states: reuse a running container, `start` a stopped one (removing and recreating it if it will not start), create an absent one, or **adopt on a name conflict** — the second line of defence for a race in another process that the lock cannot see. **Labels are the durable truth**: written at create, selected on at purge, with values passing through when short and safe and becoming a digest otherwise — never a truncation, because two scopes sharing a prefix would let one conversation's purge delete another's containers, and the mapping must be identical on both sides or the purge quietly selects nothing. `dispose_scope` sweeps the engine by label, removing containers, then their proxies, then their networks; the in-process registry is a fallback for when the listing fails, never the truth. Teardown never raises. A timed-out `exec` **discards the sandbox** — killing the client does not reach the process inside the container and there is no per-command handle — while a *cancelled* call reaps only the host-side child and leaves the sandbox running.

## The acceptance gate

`test_docker_e2e.py` runs against a **real engine on every pull request** — the only real-backend suite a PR can run, with no subscription, no login and no disk-image import. That is why the `FILES_OUT` rollout put this backend first: ACAS is the reference for shape, Docker decides whether the surface actually works. What the suite proves that no offline test can: that a one-entry tar written to `docker cp - <name>:/` creates the intermediate directories; that an argv reaches the process without a shell and the exit code and stream split come back as the backend believes; that warm reuse, restart-after-stop and adopt-on-conflict behave against a real engine, which is where the acquire-race invariant actually lives; that `--network none` denies what it claims and the allowlist topology reaches an allowed host and fails a denied one; that the label filters return the containers a purge needs; and that a declared output stats and comes back byte-identical while an over-cap one and a symlinked one are refused.

## Status

| Decision | State | Tracking |
|---|---|---|
| The backend, its four declarations, and `FILES_OUT` from the day the package existed | shipped | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) open as the `FILES_OUT` tracking issue; the docker item landed first, as the gate |
| No `FILES_LIST` — no engine-level enumeration primitive | by design | — |
| `FILES_DELETE` over `rm`, held to the ten probes | shipped | — |
| The egress proxy is a byte copy of wslc's, pinned by test rather than hoisted into core | shipped, by maintainer ruling | — |
| Live e2e on every pull request as the repository's acceptance gate | shipped | — |
| Shared egress probes across backends, so this topology is not the only one ever measured | open | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) open |
| A guest-platform axis a kind can declare and match | open | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) open |
