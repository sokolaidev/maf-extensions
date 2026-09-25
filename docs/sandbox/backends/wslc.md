# WSLC

With `credential_gateway=CredentialGateway(provider, max_lifetime_seconds=300)` and a rebuilt packaged proxy image, WSLC supports [credentials for guest HTTP](../hosts.md#credentials-for-guest-http-requests). Each acquisition gets a fresh container and gateway bound to the trusted user scope, agent, call and runtime generation. The gateway holds the bearer tokens and enforces exact HTTPS origins, method/path rules and independent expiry. This requires the attached-authority opt-ins and a call-scoped workload; credential-bearing containers are never reused.

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
| Resource limits | `memory` and `cpus` in config, applied to the workload and its proxy, unset by default; `memory` does not limit swap |
| Not enforced | PID limit, dropped capabilities, `no-new-privileges`; `wslc container run` has no flag for them |

The default Windows Proactor event loop supports the required subprocesses. A selector event loop does not.

Acquisition checks `sh` for `EXEC`. For `FILES_IN`, it checks the external `/usr/bin/test` command, including true and false exit statuses under the root principal used for path checks. A shell builtin or a `test` elsewhere on `PATH` does not satisfy that check. `FILES_IN` also checks that the image's user can start `sh` and finds `mkdir`, `cat`, `wc`, `mv` and `rm` — the probe runs `sh -c` as that user, and so does every write, so the shell is part of the `FILES_IN` contract and not only the `EXEC` one. Working-directory setup needs root `/bin/sh`, and `mkdir`, `chown`, `ls` and `pwd` — the last a shell builtin on every shell this backend admits — but those are not probed at acquisition: setup only runs when the base is missing, which is not known until the base is walked, and probing them earlier refused images that never reach setup. Where setup leaves the base to the image's user, that user's `mkdir` is needed too, which no `EXEC` probe asks for. The creation commands check these themselves and a missing one is refused as an unsupported capability, in the same shape and at the same moment as an unresolved image user. The refusal names which one: the command marks its own answer, because the statuses an engine returns when it cannot start the shell at all overlap with the one a missing utility would use, and naming the wrong prerequisite sends a reader to the wrong place. Successful checks are cached per physical container; failed checks are retried.

## Writes and path checks

`write_file` runs one command as the image's user through `container exec`. The content arrives on stdin. The command creates missing parents, writes a sibling named for the call, checks the byte count, then moves the sibling into place. The file and any new parents belong to the image's user because that user wrote them. Existing directories keep their metadata.

A destination the image's user cannot write raises `PermissionError`. There is no root fallback. Where the image's user is root, writes reach what its own programs reach. A write this host stops discards the container, because killing the host process does not reach the command inside it: that covers a blocked guest utility hitting the deadline, and a command whose stdout reaches the read cap, which the host answers by killing it and returning. This matches `exec`.

`exec` returns at most 8 MiB of stdout and stderr together. Past that it raises `SandboxExecOutputLimitExceeded` and discards the container, as a timeout does. An `exec_bounded` caller's own budget keeps the container when it overflows.

On WSLC 2.9.12.0 a 32 MiB write took 0.31 s and a plain exec 0.11 s. The write's byte count is checked against the content length before the file is published, so an engine whose `exec` does not stream stdin refuses the write rather than publishing a short file. `container exec --interactive` is present in the CLI source from the supported 2.9.3 minimum; live evidence covers 2.9.12.0.

Path checks use the engine's copy behavior to identify missing paths and directories. For other accepted copy sources, a guest probe supplies the remaining type. A guest claim that such a source is a directory contradicts the engine and is rejected. The probe is still an image-dependent limitation.

The root probe invokes `/usr/bin/test` directly with separate arguments. A guest-writable directory earlier in `PATH` cannot supply its executable. The image must protect that executable, its dependencies and ancestor directories from the runtime user; pinning its path does not establish trust in an arbitrary image.

Each stat copies into a private host temporary directory, removed after the subprocess exits. Guest file sizes determine temporary disk use and I/O; upload and stdout limits do not bound those bytes. Host termination or failed cleanup can leave data behind. The operator must bound the host temporary filesystem.

## Working-directory setup

Acquisition creates a missing base as root where it can, because the image's user often cannot create its parents. One command runs `/bin/sh` with `PATH` set to `/usr/sbin:/usr/bin:/sbin:/bin` and `CDPATH` cleared, so an inherited `CDPATH` cannot divert a relative `cd`. It walks down from `/`, holding each directory as its working directory, and compares `pwd -P` with the path it asked for after every `cd -P`, so a component replaced by a link is refused. It runs `mkdir` for each missing directory inside the directory it holds, then gives the base to the image's user with `chown`. A refusal can leave behind the directories created before it.

**Root acts inside a directory only when that directory is root's and writable by nobody else.** That is the reach rule's condition for acting with more authority than the guest. A link check cannot tell one real directory from another, and the image's user can rename a real directory into any name it can write beside; inside a directory only root can write, there is nothing for it to replace. The command reads each held directory's owner with `test -O`, a builtin of every shell this backend admits, and its mode with `ls -ld` from the pinned `PATH`; a mode `ls` does not report fails the check. A write an ACL grants shows in the group bits, which carry the ACL's mask. The answers come from the image's own `/bin/sh` and `ls`, which the root command already runs.

At the first directory that fails — one root does not own, or any group- or world-writable directory, `/tmp` included — the command stops without creating anything more. A second command then creates the base as the image's user with `mkdir -p`. That user's own permissions bound where a swap can send it, as they bound a write. What it creates belongs to that user, intermediate directories included, and a setgid parent's group stays on them. Where that user cannot create the base, acquisition raises `PermissionError`: point `work_dir` at a base that user can create, or at one whose existing directories are root's alone.

The base goes to the image's user in the command that creates it, and **only for a base this acquire created**: a directory that was already there keeps its owner, whichever path named it, because `acquire` preserves the contents, ownership and permissions it finds. Chowning an existing base would hand the image's user a directory the host never offered — with `work_dir=/etc`, `/etc` itself. Directories root creates above the base stay root's.

A setup that fails, times out or is cancelled removes the container, so a half-prepared base goes with it.

Numeric IDs come from container inspection. Named users or missing groups require bounded guest `id` replies. An empty user means root. An `id` this host cannot account for — its deadline expired, or its output reached the read cap — discards the container and fails the acquire: the command may still be running in there, and the sandbox was about to be handed to a caller. An `id` that answers something unusable is an ordinary unresolved identity and keeps the container. Unresolved identity refuses any capability that has to *create* a base — a directory this backend creates has to be given to someone, so `EXEC` and `FILES_IN` alike need it when the base is missing, and the refusal comes before anything is created. An existing base needs none of it: writes run as the image's user and stamp nothing, so an image whose base is already there is served whatever its user resolves to. Identity is checked on each acquire.

<a id="write-checkcopy-residual"></a>

## Parent swaps

![WSLC checks the path, and a guest can then replace a checked parent with a link or a different real directory before the placement command starts. A write runs as the image's user, so the swap can send it only where that user can already write; a root-only target refuses it. Working-directory setup runs as root only inside directories that are root's and writable by nobody else, where a swap has nothing to replace, and refuses a link besides; anywhere else the image's user creates the base, bounded the same way as a write. Both cases stay inside the container.](../assets/wslc-write-window.svg)

The path check and the placement are separate commands. A guest can replace a checked parent between them, with a link or with a different real directory. A link present during the check is refused.

A write runs as the image's user, so a swap can send it only where that user can already write. This is a bound, not atomicity: another place that user can write is still reachable. Setup runs as root only inside directories no swap can replace, and refuses a link besides, including one planted where a directory was missing. Anywhere else it creates the base as the image's user, bounded the same way as a write, so a real directory renamed into the same name receives only what that user could create there.

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

## Resource limits and hardening

`WslcSandboxConfig(memory="512M", cpus=1.5)` sets `--memory` and `--cpus` on the workload container and on its egress proxy, because the guest drives the proxy's load. Both are unset by default. The engine refuses a value it cannot apply, such as more CPUs than the session has, and acquisition raises `RuntimeError` with its message.

`memory` limits resident memory, not swap. WSLC sets no swap limit, and each session has its own swap file ([microsoft/WSL#41438](https://github.com/microsoft/WSL/issues/41438)). On WSLC 2.9.13.0 a workload limited to 256 MiB allocated and touched 1 GiB without being killed; about 780 MiB went to swap.

`wslc container run` has no `--pids-limit`, `--cap-drop` or `--security-opt` ([microsoft/WSL#41545](https://github.com/microsoft/WSL/issues/41545)). A WSLC workload and its proxy therefore keep the engine's default capability set, run with `no-new-privileges` off and can start processes without a limit. The engine does apply a seccomp filter. The Docker backend sets `no-new-privileges` and a PID limit on both containers, drops every capability from its proxy and can drop them from the workload.

`--ulimit nproc` is not a substitute for a PID limit. It does not apply to root, and it counts every process its user ID runs in every container of the session, so one sandbox's processes use up another's allowance. The backend does not set it.

Both flags are in the CLI source from the supported 2.9.3 minimum; live evidence covers 2.9.13.0.

## Network policy

![A CLOSED workload has no network. A nonempty ALLOWLIST connects the workload to an internal network and iron-proxy; the proxy also joins an outbound network. Allowed hosts, HTTP methods, paths and resolved addresses are checked there. Proxy decisions are attributed to the sandbox when the proxy is removed. Docker's additional unaddressed-bridge check belongs to Docker, while WSLC uses its own engine network behavior.](../assets/container-egress.svg)

With no proxy image, only `CLOSED` is available. With one, `ALLOWLIST` uses an internal network and a proxy connected to the outbound network. The proxy listens only on its internal-network address, so other containers on the outbound network cannot use it. An empty allowlist uses the closed setup. The proxy terminates guest TLS, checks host, method and path, and validates the upstream certificate. Its per-sandbox CA certificate is installed at the fixed guest path `/maf-sandbox-proxy-ca.crt` and named in `SSL_CERT_FILE`, `CURL_CA_BUNDLE` and `REQUESTS_CA_BUNDLE`; its key stays in the proxy. Public HTTP is denied on every port. Listed private endpoints use TLS unless `allow_private_http=True` is set for development or test. The outbound dial checks the resolved address and denies loopback, link-local, metadata, gateway and proxy interface addresses. See [network policy](../network.md) for the full contract.

The proxy gets the workload's memory and CPU limits, and like the workload it has no PID limit, no `no-new-privileges` and the engine's default capability set. See [resource limits and hardening](#resource-limits-and-hardening).

WSLC 2.9.12 creates IPv4-only bridge endpoints even when given an IPv6 subnet: network inspection reports `EnableIPv6=false`, and the container has no IPv6 address or route. Private IPv6 HTTP/TLS reachability cannot be verified with this topology. IPv6 loopback, link-local and metadata denials have been measured through the adapter. The live IPv6 transport test checks network readiness first and reports an explicit skip when IPv6 is disabled; a skipped test is not evidence of reachability. A host with a working ULA IPv6 network can supply its name through `MAF_SANDBOX_WSLC_E2E_IPV6_NETWORK`; the test removes its own containers and leaves that network in place. [#1407](https://github.com/sokolaidev/maf-extensions/issues/1407) tracks the remaining verification.

Every acquire rebuilds the proxy. Before removing a proxy, the backend reads its JSON audit records. Persistent labels carry the exact key for attribution, including call identity, up to 4,096 encoded bytes.

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
| Parent swaps at placement | Bounded — writes run as the image's user; setup runs as root only where nothing can be swapped, and as the image's user elsewhere | [#1338](https://github.com/sokolaidev/maf-extensions/issues/1338) (closed) by [#1380](https://github.com/sokolaidev/maf-extensions/pull/1380) (merged) and [#1400](https://github.com/sokolaidev/maf-extensions/pull/1400) (merged); held no-follow upload is [microsoft/WSL#41594](https://github.com/microsoft/WSL/issues/41594) (open) |
| Output reads and listing | Withheld pending an adequate engine interface | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open), [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (open), [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (open) |
| Delete, reclaim and reset | Withheld | [Cleanup contract](../tool-call.md) |
| Container resource and privilege limits | `memory` and `cpus` applied to the workload and the proxy; PID limit, capability drop and `no-new-privileges` wait on the CLI | [#1452](https://github.com/sokolaidev/maf-extensions/issues/1452) (closed) and [#1455](https://github.com/sokolaidev/maf-extensions/issues/1455) (closed) by [#1474](https://github.com/sokolaidev/maf-extensions/pull/1474) (merged); [microsoft/WSL#41545](https://github.com/microsoft/WSL/issues/41545) (open), [microsoft/WSL#41438](https://github.com/microsoft/WSL/issues/41438) (open) |
| Temporary host disk use during stat | Explicit limit; requires host quotas | [Package README](../../../packages/maf-sandbox-wslc/README.md) |
| Operator retention | Implemented; maintenance coordination required | [Operations](../operations.md) |
