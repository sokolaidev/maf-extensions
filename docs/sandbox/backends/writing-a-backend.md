# Writing a backend

A backend implements `SandboxBackend` and returns objects that implement `Sandbox`. It owns resource creation, execution, files and cleanup. Kinds use only these protocols.

Use the [protocol definitions](../../../packages/maf-sandbox/src/maf_sandbox/_protocol.py) for signatures and the [backend index](README.md) for comparisons. Package READMEs own installation and configuration.

![Backend development starts with isolation and supported capability declarations. The implementation provides every protocol member, using explicit refusals for unsupported operations. Offline tests check routing and failure handling. Shared conformance probes then exercise the real provider, followed by backend-specific boundary tests. Only the behavior established by those checks is declared and documented. An unsupported result or skipped required probe does not establish support.](../assets/backend-authoring-flow.svg)

## 1. Declare the supported contract

Set the required `isolation` member and one `BackendDeclarations` object. The router reads declarations before acquisition. The [declaration table](README.md#backend-declarations) lists defaults; silence does not mean unlimited support.

Declare only operations you can enforce. A missing capability is refused, not emulated. Every protocol member must still exist; unsupported methods raise `NotImplementedError` with the backend name and reason.

The host's isolation floor applies at router construction. A stricter spec is checked before work. Egress mode, file limits, OS family, sharing and any attached-identity requirements are checked too. See [policy and isolation](../policy-isolation.md).

Use `requires_exclusive_admission` when one call must own a sandbox through delivery and cleanup. Implement the backend admission hook when ownership must span multiple routers or event loops. Bound admission and release it on cancellation.

## 2. Establish ownership and storage

Key physical sandboxes by `(SandboxKey, kind)`. Serialize get-or-create, or use provider uniqueness that resolves competing creates safely. Reuse must preserve the selected policy and storage base.

Write ownership labels at creation. Hash long values rather than truncating them. Provider-backed purge must discover resources independently of the creator's memory. A process-owned runtime must instead document and enforce its owner-routing requirement.

`dispose(key)` removes all kinds unless filtered. `dispose_scope` removes the selected scope and conversation. Return structured failures instead of raising. Retain failed targets for retry, and use physical IDs so stale cleanup cannot delete a replacement.

Bind each sandbox to a private storage base. `spec.work_dir=None` delegates allocation; an explicit value requires that exact base. Preserve existing contents and permissions. Refuse obstructed ancestry or a different base on warm reuse.

For filesystem backends, `ensure_guest_work_dir` prepares the base for `EXEC` and file capabilities on cold and warm acquire. Supply a native resolver, no-follow stat and directory creation. POSIX implementations can use `posix_work_dir_ancestors` and `resolve_guest_working_directory`. Runtime-only specs need no directory. The creation you supply carries the same authority obligation as a write: the ancestry check and the creation are separate calls, so bound creation to what the guest could make itself, or refuse when a missing directory cannot be created within that authority — a host-authority creation a swapped parent can redirect exceeds the guest's reach.

Relative working directories resolve against the base; `"."` names it. Keep command text and argv unchanged. Relative reclaim targets must resolve to a child of the working directory, then pass the backend's native placement checks.

Document provider retention after host failure. Operator helpers can discover and delete resources, but credentials, maintenance coordination and scheduling belong to the deployment. Do not start a scheduler inside the backend.

## 3. Implement the methods

The four lines under each method name summarize the contract, shared helper, common mistake and evidence. Named probes are checked by the repository, but passing a naming check is not proof that a provider conforms.

### `instance_id`

- **Owes:** A nonempty physical-instance identifier, stable across wrappers and changed after replacement or reset.
- **Use:** The engine's container or sandbox ID. A runtime may own an equivalent generation identity.
- **Never:** Use a derived name, Python wrapper identity or a baseline captured after workload execution.
- **Proved by:** Adapter tests for reuse and replacement, plus router tests for adoption and cleanup. No shared probe exists.

### `write_file`

- **Owes:** Confined writes, parent creation, exact bytes and UTF-8 encoding of strings. Refuse links, escapes and the working directory itself. Inputs must remain writable by the guest.
- **Use:** `confine_resolve_guest_write_path`, backed by an unconfined, no-follow engine stat. Supply the actual encoding, directory creation and write.
- **Never:** Treat ownership stamping as proof of placement authority. If access has more authority than the guest, close the path-replacement window or state the remaining limit explicitly.
- **Proved by:** `a-write-lands-and-reads-back`, `bytes-survive-the-round-trip`, `str-content-is-utf8`, `parents-are-created`, `a-linked-destination-is-refused-not-followed`, `a-write-leaves-nothing-beyond-the-guest`.

### `exec`

- **Owes:** Run within `timeout` at the requested working directory. Return raw stdout/stderr bytes and the exit status. `TimeoutError` means the caller's execution deadline expired.
- **Use:** Native argv where available; otherwise quote argv as the protocol requires. Keep display decoding in the result's text views.
- **Never:** Claim `producer_owns_stderr` while guest text remains in stderr. If the producer owns that stream, move guest stderr to stdout. Do not report an unrelated shorter limit as the caller's timeout.
- **Proved by:** `an-argv-sequence-runs`, `exit-code-fidelity`, `argv-is-quoted`, `working-directory-is-honoured`, `streams-stay-separate`, `exec-byte-fidelity`, `a-timeout-raises-timeout-error`.

### `reclaim`

- **Owes:** Remove the selected directory and descendants within `timeout`. Missing is success. A path swap must not let cleanup delete anything the guest could not delete.
- **Use:** Guest-authority removal. Raised removal needs established ancestor ownership, checked with `path_ancestors_are_host_owned`; choose `empty_means_host_owned` explicitly.
- **Never:** Trust a framework-chosen path without placement and authority checks. A relative target must be a child of the working directory. Declare `RECLAIM` only when the removal is safe.
- **Proved by:** `a-created-directory-is-gone`, `nested-content-goes-with-it`, `a-link-inside-is-unlinked-not-followed`, `a-missing-directory-is-success`, `an-absent-working-directory-still-succeeds`.

### `reset`

- **Owes:** Restore the pre-input baseline, including files and execution state, within `timeout`. Preserve key/kind addressing and change the instance identity after success. Raise on failure so cleanup can escalate to disposal.
- **Use:** A provider restore or replacement from a baseline captured before the first workload. Declare `SNAPSHOT` only when this full contract holds.
- **Never:** Restore files while leaving workload processes or mutable runtime state behind.
- **Proved by:** Hyperlight tests for globals, builtins, outputs and failure cleanup. A shared reset suite remains unavailable.

### `stat_file`

- **Owes:** A `SandboxEntry` with a path relative to the working directory, or `None` for an absent path. Describe the final link itself; check its parents without following links.
- **Use:** `confine_resolve_guest_read_path` and engine metadata. Archive backends can use `tar_header_from_block` and `sandbox_entry_from_tar_header`.
- **Never:** Treat a provider failure as absence, or follow the final link to report its target. State which filesystem the provider can actually observe.
- **Proved by:** `a-link-is-named-a-link`, `stat-through-a-linked-parent`, `a-linked-working-directory`, `a-linked-ancestor-of-the-working-directory`, `a-plain-parent-is-not-an-escape`.

### `read_file`

- **Owes:** Exact bytes from a regular file. Refuse links and files above `max_bytes` with `SandboxTransferCapExceeded`.
- **Use:** `confine_resolve_guest_read_path`, then check the final entry kind. Non-regular entries raise `OSError`.
- **Never:** Decode, truncate or return a short read as success. Bound transfer and buffering where the provider allows it; still check the returned byte count.
- **Proved by:** `a-legitimate-read-still-works`, `a-link-is-never-read`, `read-through-a-linked-parent`, `a-linked-working-directory`, `a-linked-ancestor-of-the-working-directory`.

### `list_dir`

- **Owes:** Direct children, with paths relative to the working directory. Listing `sub` returns `sub/file`, not just `file`. Report links as links.
- **Use:** `confine_resolve_guest_list_path`, which checks the target directory as well as its ancestors.
- **Never:** Enumerate through a link or hide a link's type. Bound any provider work required to produce the listing.
- **Proved by:** `listing-a-linked-directory`, `listing-through-a-linked-parent`, `listing-under-a-linked-ancestor`, `a-listing-names-its-links`.

### `remove`

- **Owes:** Missing paths succeed. Unlink final links without following them. Directories require `recursive=True`; the working directory itself is refused. Removal must stay within the guest's deletion authority.
- **Use:** `confine_resolve_guest_delete_path`, which checks ancestors and leaves the target for unlinking.
- **Never:** Resolve the final link or raise deletion authority without establishing safe ancestor ownership.
- **Proved by:** `a-removal-removes`, `a-missing-path-is-success`, `a-link-is-removed-never-followed`, `a-directory-needs-recursive`, `the-working-directory-is-refused`, `a-removal-takes-nothing-beyond-the-guest`.

### `run_code`

- **Owes:** The documented runtime semantics, exact result streams and a deadline that includes queue time. Expiry before submission uses `SandboxQueuedTimeout`; execution expiry uses `TimeoutError`.
- **Use:** A runtime contract owned by the backend. Otherwise raise `NotImplementedError` and withhold `RUN_CODE`.
- **Never:** Infer a runtime guarantee from an arbitrary image reference. Document imports, persistent state, result semantics and cleanup.
- **Proved by:** Backend tests for results, state, errors, deadlines and cancellation. Hyperlight runs real guest tests; the fake scripts results. No shared runtime suite exists.

## 4. Verify authority and bounds

Use engine metadata for path checks wherever available. When guest inspection is unavoidable, use `stat_by_asking_the_guest` or `stat_by_asking_the_guest_as_root` and state that dependency in the package README. The shared helpers keep link checks in the required order.

A path check and a later operation are separate unless the provider makes them atomic or prevents intervening mutation. Docker pauses guest processes around archive operations. WSLC writes as the image's user, which bounds a swap, and creates its base as root only inside directories that are root's and writable by nobody else, or as the image's user elsewhere. ACAS native reads retain an explicit race. Passing an ownership probe does not close those windows.

The file view must match the storage being documented. For example, [Docker archives](docker.md#the-pull-surface-one-tar-read-twice) cover the root filesystem, not guest tmpfs. A missing result in one view does not establish absence in another.

Implement optional `BoundedExec.exec_bounded` for a combined stdout/stderr transport budget enforced before buffering. Overflow raises `SandboxExecOutputLimitExceeded`, never partial success. Process observation and descendant cleanup need this surface. Closing a host transport alone does not prove that guest processes stopped.

Document extra bounded cleanup time after execution failure. See [exec output](../exec-output.md), [file capabilities](../capabilities.md) and [network policy](../network.md).

## 5. Run conformance against the provider

Offline tests cover declarations, router acceptance/refusal, ownership and failure paths. Also check `isinstance(backend, SandboxBackend)` and keep a `tuple[SandboxBackend, type[Sandbox]]` binding under `TYPE_CHECKING` for full signature checking.

Use only declared package dependencies. Backends must not import `agent_framework` or kind packages.

Real-provider tests must exercise the operations being claimed. A `ConformanceSubject` supplies setup and observation beyond the protocol. `PosixGuestSubject` suits a guest with the required shell utilities; a missing utility is a harness failure, not a provider verdict.

| Check | Required use |
|---|---|
| `assert_storage_base_conformance` | Fresh default and explicit bases; also test warm reuse, removed bases and conflicting overrides. |
| `assert_files_in_conformance`, `assert_exec_conformance`, `assert_files_delete_conformance` | Run declared gates; assert the suite's refusal for withheld capabilities. Required probes must not silently skip. |
| `assert_files_out_conformance` | Run when stat and read are implemented. |
| `assert_reclaim_conformance` | Run when declared; otherwise assert refusal before setup. Its observation hooks support runtime-only subjects. |
| `assert_reach_conformance` | Run for every subject; individual probes gate on the capabilities they need. Use a non-root guest that owns its working directory for meaningful authority checks. |
| `measure_files_delete_probes` | Record findings for an implemented but undeclared delete operation. |
| `assert_egress_conformance` | Required for `ALLOWLIST` plus `EXEC`; configure allowed and denied URLs. |
| `assert_call_scope_conformance` | Check separate call instances and disposal when declaring `CALL`. |

Run exec probes last when their timeout can dispose the sandbox. The POSIX harness requires commands such as `sh`, `cat`, `printf`, `pwd`, `sleep`, `mkdir`, `ln`, `test` and `rm`. Egress probes additionally require `curl`.

Some file suites verify through `exec`, and delete probes also use uploads. They cannot currently prove declared file support on a backend lacking those channels. Record the harness gap and use backend-specific measurements; do not report skipped required probes as conformance.

## Status

| Area | State | Reference |
|---|---|---|
| Protocol methods, path helpers and named probes | Implemented | [Capabilities](../capabilities.md) |
| Provider path-replacement races | Backend-specific; ACAS reads retain limits, WSLC writes and setup reach nothing past the image's user | [#456](https://github.com/sokolaidev/maf-extensions/issues/456) (open), [ACAS](acas.md), [WSLC](wslc.md) |
| Shared runtime and reset suites | Not implemented; Hyperlight has provider-specific tests | [Hyperlight](hyperlight.md) |
| File suites without exec/upload dependencies | Harness gap | untracked |
