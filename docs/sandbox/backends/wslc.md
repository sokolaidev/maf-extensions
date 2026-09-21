# WSLC

WSLC runs Linux containers on Windows through the `wslc.exe` CLI included with WSL. It supports command execution and file upload. Use the [package README](../../../packages/maf-sandbox-wslc/README.md) for setup.

## Supported contract

| Setting | Value |
|---|---|
| Host | Windows with WSL 2.9.3 or later; an event loop that supports subprocesses |
| Isolation | `CONTAINER`; the host must set `min_isolation=Isolation.CONTAINER` |
| Capabilities | `EXEC`, `FILES_IN` |
| Network | `CLOSED`; `ALLOWLIST` and egress observation with a configured proxy image |
| Guest OS | POSIX |
| Sharing | `CONVERSATION`, `CALL` |
| Transfer limits | `DEFAULT_SANDBOX_LIMITS` |
| Cleanup | Disposal; no `RECLAIM` or `SNAPSHOT` |

The default Windows Proactor event loop supports the required subprocesses. A selector event loop does not.

Acquisition checks `sh` for `EXEC`. For `FILES_IN`, it checks the external `/usr/bin/test` command, including true and false exit statuses under the root principal used for path checks. A shell builtin or a `test` elsewhere on `PATH` does not satisfy that check. `FILES_IN` also checks that the image's user finds `mkdir`, `cat`, `wc`, `mv` and `rm`. Successful checks are cached per physical container; failed checks are retried.

## Writes and path checks

`write_file` runs one command as the image's user through `container exec`. The content arrives on stdin. The command creates missing parents, writes a sibling named for the call, checks the byte count, then moves the sibling into place. The file and any new parents belong to the image's user because that user wrote them. Existing directories keep their metadata.

A destination the image's user cannot write raises `PermissionError`. There is no root fallback. Where the image's user is root, writes reach what its own programs reach. A write that times out — a blocked guest utility, say — discards the container, because killing the host process does not reach the command inside it; this matches `exec`.

On WSLC 2.9.12.0 a 32 MiB write took 0.31 s and a plain exec 0.11 s. The write's byte count is checked against the content length before the file is published, so an engine whose `exec` does not stream stdin refuses the write rather than publishing a short file. `container exec --interactive` is present in the CLI source from the supported 2.9.3 minimum; live evidence covers 2.9.12.0.

Path checks use the engine's copy behavior to identify missing paths and directories. For other accepted copy sources, a guest probe supplies the remaining type. A guest claim that such a source is a directory contradicts the engine and is rejected. The probe is still an image-dependent limitation.

The root probe invokes `/usr/bin/test` directly with separate arguments. A guest-writable directory earlier in `PATH` cannot supply its executable. The image must protect that executable, its dependencies and ancestor directories from the runtime user; pinning its path does not establish trust in an arbitrary image.

Each stat copies into a private host temporary directory, removed after the subprocess exits. Guest file sizes determine temporary disk use and I/O; upload and stdout limits do not bound those bytes. Host termination or failed cleanup can leave data behind. The operator must bound the host temporary filesystem.

## Working-directory setup

Acquisition creates a missing base as root, because the image's user often cannot create its parents. One command runs `/bin/sh` with `PATH` set to `/usr/sbin:/usr/bin:/sbin:/bin` and `CDPATH` cleared, so an inherited `CDPATH` cannot divert a relative `cd`. It enters the deepest existing directory with `cd -P` and confirms with `pwd -P` that the directory is where the check found it. It then runs `mkdir` for each missing directory inside the directory it holds, confirming each one the same way. A link swapped in after the check is refused rather than followed. A refusal can leave behind the directories created before it.

A second held command gives the base to the image's user, on every acquire. It is held the same way — `cd -P` then a `pwd -P` comparison — so a base swapped for a link is refused rather than chowned through. Running it every time is deliberate: it is a no-op on a base the guest already owns, and it repairs a base a partial setup left root-owned, so a later acquire never returns a base the guest cannot write. The directories above the base stay root's.

Numeric IDs come from container inspection. Named users or missing groups require bounded guest `id` replies. An empty user means root. Unresolved identity refuses `FILES_IN` at acquisition. Identity is checked on each acquire.

<a id="write-checkcopy-residual"></a>

## Parent swaps

![WSLC checks the path, and a guest can then replace a checked parent with a link before the placement command starts. A write runs as the image's user, so the swap can send it only where that user can already write; a root-only target refuses it. Working-directory setup runs as root, but it confirms each directory with cd -P and pwd -P before creating anything inside it, so a swapped parent or a planted link is refused. Both cases stay inside the container.](../assets/wslc-write-window.svg)

The path check and the placement are separate commands. A guest can replace a checked parent with a link between them. A link present during the check is refused.

A write runs as the image's user, so a swap can send it only where that user can already write. This is a bound, not atomicity: another place that user can write is still reachable. Setup runs as root, but a swapped parent or a link planted where a directory was missing is refused.

Cancelling before the placement command starts writes nothing. Cancelling after it starts is not a rollback. The host closes stdin, and the command then refuses short content. A write whose bytes had all arrived still lands. A `.maf-<hex>.part` sibling can remain if the command itself is interrupted.

These are container-internal permission boundaries, not an escape into the Windows host filesystem. The [WSLC research record](../research/wslc-backend.md) contains the controlled measurements.

## Unsupported operations

| Operation | Reason |
|---|---|
| `stat_file`, `read_file`, `list_dir` | Container copy has no stdout archive form. A symlink source can appear as an empty successful host copy, with no trustworthy header for type and size. |
| `remove` | No implemented and validated delete operation. Guest-authority removal would need its own conformance checks. |
| `reclaim` | Engine metadata cannot establish safe ancestor ownership for raised recursive removal. |
| `reset` | No snapshot contract. |
| `run_code` | The image owns its runtime. A kind can invoke a known interpreter through `exec`. |

These methods raise `NotImplementedError`. Their capabilities, including `HOST_TOOLS`, are not declared. The router refuses a kind that requires them.

## Network policy

![A CLOSED workload has no network. A nonempty ALLOWLIST connects the workload to an internal network and a CONNECT proxy; the proxy also joins an outbound network. Allowed hosts and destination addresses are checked there. Proxy decisions are attributed to the sandbox when the proxy is removed. Docker's additional unaddressed-bridge check belongs to Docker, while WSLC uses its own engine network behavior.](../assets/container-egress.svg)

With no proxy image, only `CLOSED` is available. With one, `ALLOWLIST` uses an internal network and a proxy connected to the outbound network. An empty allowlist uses the closed setup. The proxy checks hostnames and refuses non-global destination addresses.

Every acquire rebuilds the proxy. Before removing a proxy, the backend reads its `ALLOW` and `DENY` log lines. Persistent labels carry the exact key for attribution, including call identity, up to 4,096 encoded bytes.

Larger keys still work, but scope sweeps cannot recover their attribution. Key-addressed operations can use the caller's key when ownership labels agree. Invalid labels emit no event. Failed removal emits none; overlapping cleanup can duplicate a window. Observation is therefore incomplete. See [network policy](../network.md) and [observability](../observability.md).

## Lifecycle and cleanup

Acquisition serializes get-or-create per event loop, key and kind. Derived names include scope, thread, agent, kind, network policy and any call ID. Creation writes ownership labels and the selected storage base. Warm reuse refuses a changed or missing base.

Disposal discovers resources through engine labels, with local records as a fallback. A kind filter narrows key disposal. A conversation purge reaches every call beneath it. Long selectors are hashed rather than truncated.

The router disposes after every call because neither reclamation nor reset is declared. The logged `guest_principal` comes from guest `id -u` and is diagnostic only. It cannot authorize a privileged operation.

## Operator retention

`reap(stopped_for, *, scope=None)` discovers resources from the engine. It retains running workloads. A stopped workload expires from `State.FinishedAt`; a never-started container uses its creation time.

Successful workload deletion permits removal of its proxy and network. Orphan proxies use creation age. Network-only leftovers use the backend's creation-request label; old networks without that label require manual cleanup. These infrastructure ages do not measure workload inactivity.

The operator must pause and drain acquisitions, restarts and resource changes in the selected scopes. Sweeps must not overlap. Containers are removed by immutable ID without force. Networks are rechecked and removed without disconnecting endpoints, but their name-based deletion cannot make inspection and removal atomic.

The backend starts no scheduler. See the [retention example](../../../packages/maf-sandbox-wslc/README.md#operator-retention) and [operations](../operations.md).

## Status

| Area | State | Tracking |
|---|---|---|
| Commands, guest-owned inputs, call scope and disposal | Implemented | [Package README](../../../packages/maf-sandbox-wslc/README.md) |
| Parent swaps at placement | Bounded for writes by the image's user; refused for setup | [#1338](https://github.com/sokolaidev/maf-extensions/issues/1338) (closed) by [#1380](https://github.com/sokolaidev/maf-extensions/pull/1380) (merged) |
| Output reads and listing | Withheld pending an adequate engine interface | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open), [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (open), [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (open) |
| Delete, reclaim and reset | Withheld | [Cleanup contract](../tool-call.md) |
| Temporary host disk use during stat | Explicit limit; requires host quotas | [Package README](../../../packages/maf-sandbox-wslc/README.md) |
| Operator retention | Implemented; maintenance coordination required | [Operations](../operations.md) |
