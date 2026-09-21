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

Acquisition checks `sh` for `EXEC`. For `FILES_IN`, it checks the external `/usr/bin/test` command, including true and false exit statuses under the root principal used for path checks. A shell builtin or a `test` elsewhere on `PATH` does not satisfy that check. Successful checks are cached per physical container; failed checks are retried.

## Writes and path checks

Uploads use an archive extracted by the engine. Numeric guest IDs come from container inspection. Named users or missing groups require bounded guest `id` replies. An empty user means root; unresolved identity refuses upload. Identity is checked on each acquire.

Files and missing directories at or below the working directory receive the guest uid/gid. Existing directories retain their metadata. This lets a non-root guest edit inputs and create files beside them. It does not reduce the engine's authority while placing those files.

Path checks use the engine's copy behavior to identify missing paths and directories. For other accepted copy sources, a guest probe supplies the remaining type. A guest claim that such a source is a directory contradicts the engine and is rejected. The probe is still an image-dependent limitation.

The root probe invokes `/usr/bin/test` directly with separate arguments. A guest-writable directory earlier in `PATH` cannot supply its executable. The image must protect that executable, its dependencies and ancestor directories from the runtime user; pinning its path does not establish trust in an arbitrary image.

Each stat copies into a private host temporary directory, removed after the subprocess exits. Guest file sizes determine temporary disk use and I/O; upload and stdout limits do not bound those bytes. Host termination or failed cleanup can leave data behind. The operator must bound the host temporary filesystem.

<a id="write-checkcopy-residual"></a>

## Write path race

![WSLC checks an existing parent and then submits an archive. Between those steps, a guest can replace that parent with a link. Root-authority extraction follows the changed parent and can place the upload in a protected directory inside the container. Stamping the guest uid and gid changes ownership of the result, not the authority used to place it. The backend has no supported container freeze or constrained upload that closes this window.](../assets/wslc-write-window.svg)

An existing checked parent can be replaced before archive extraction. The write can then reach a protected directory that the guest cannot write. This is a container-internal permission boundary, not evidence of escape into the Windows host filesystem.

The same race applies to creation of missing children and working-directory setup. Explicit archive directory entries can replace a link at that exact missing path. They do not protect an existing prefix omitted from the archive to preserve its metadata.

A link present during checking is refused. A later swap is not prevented. Cancellation before submission writes nothing; cancellation after submission cannot roll back engine extraction.

The supported engine interface provides no constrained upload, guest-authority copy or container freeze. `SIGSTOP` does not prevent new exec requests. Hosts requiring protection from concurrent guest path changes must avoid this upload mechanism or select another backend. The [WSLC research record](../research/wslc-backend.md) contains the controlled measurements.

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
| Write path race | Open; root-authority extraction can follow a replaced parent | [#456](https://github.com/sokolaidev/maf-extensions/issues/456) (open), [microsoft/WSL#41594](https://github.com/microsoft/WSL/issues/41594) (open) |
| Output reads and listing | Withheld pending an adequate engine interface | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open), [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (open), [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (open) |
| Delete, reclaim and reset | Withheld | [Cleanup contract](../tool-call.md) |
| Temporary host disk use during stat | Explicit limit; requires host quotas | [Package README](../../../packages/maf-sandbox-wslc/README.md) |
| Operator retention | Implemented; maintenance coordination required | [Operations](../operations.md) |
