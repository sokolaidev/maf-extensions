# Hyperlight writable files: the prerequisite for the adapter

> Exploration and proposed upstream prerequisite for [#382](https://github.com/sokolaidev/maf-extensions/issues/382), based on live probes on 2026-09-13. The CodeAct runtime variant has shipped in [#1199](https://github.com/sokolaidev/maf-extensions/pull/1199); this record checks whether published Hyperlight wheels can honor its file contract. No backend package is delivered by this record.

The matched Hyperlight 0.7.0 Python stack executes code on Windows WHP when configured for one VM per process. Its writable filesystem does not yet satisfy the proposed adapter: every execution deletes the writable files before the program starts. That includes inputs staged by the host and outputs from the preceding execution. Read-only `/input` survives, including across snapshot restore, but cannot implement the core's writable `FILES_IN` contract.

This is a prerequisite for the file-enabled adapter, independent of [#369](https://github.com/sokolaidev/maf-extensions/issues/369). An initial package declaring only `RUN_CODE` and `SNAPSHOT` remains possible. This record recommends resolving the filesystem contract before building the file-enabled package, rather than implementing staging around an implicit deletion.

## Verified environment and reproducer

The live environment was Windows 11, AMD64, WHP, CPython 3.13.12. All three distributions were installed at exactly 0.7.0: `hyperlight-sandbox`, `hyperlight-sandbox-backend-wasm`, and `hyperlight-sandbox-python-guest`. The upstream release source is [v0.7.0, commit 6ae78065](https://github.com/hyperlight-dev/hyperlight-sandbox/tree/6ae78065617d5603c1dd5fdbb63d62d8201ac68c). A second isolated environment carried the matched 0.4.0 trio for comparison; no workspace dependency was changed.

[The executable probe](hyperlight-filesystem-probe.py) carries those exact 0.7.0 pins in its PEP 723 metadata. Run it on a supported hypervisor host:

```sh
uv run --script docs/sandbox/research/hyperlight-filesystem-probe.py
```

It uses a disposable child process, a 45-second outer deadline, and temporary directories cleaned after the child exits. A zero exit code means the observations were collected, not that the filesystem passed conformance. The JSON records guest failures separately from worker failures. It does not contact an external service or register a host tool.

On Windows, the default probe sets `HYPERLIGHT_MAX_SURROGATES=0` in the child environment and retains the `WinHvPlatform.dll` handle for the guest lifetime. Pass `--native-default` to compare the native surrogate default. The matched 0.6.0 and 0.7.0 stacks failed during surrogate-manager creation with that default on this host; the 0.7.0 stack succeeded in single-VM mode. The precise cause of the surrogate-manager failure was not established. This is a supported host setting in [Hyperlight 0.17.0's `surrogates_disabled`](https://github.com/hyperlight-dev/hyperlight/blob/388ea8ebd88639a062f7a87f477db2930605a307/src/hyperlight_host/src/hypervisor/surrogate_process_manager.rs), and matches a worker owning exactly one VM. It is not a claim that default surrogate mode fails on every Windows host.

## What the guest did

| Probe | Matched 0.7.0 on WHP | Consequence |
| --- | --- | --- |
| Execute `print("hello")` | Exit 0, `hello` on stdout | The pinned guest and backend are compatible in single-VM mode. |
| Stage a host file under the writable output directory, then read `/output/staged.txt` | Guest `FileNotFoundError`; the host confirms the file was deleted | Direct host staging cannot implement `write_file` for the next execution. |
| Read a staged `/input/source.txt` | Reads the expected bytes | Input passthrough works. |
| Edit `/input/source.txt` from Python | Guest `PermissionError` | Input passthrough alone does not meet the writable-input contract. |
| Create `/output/created.txt`, collect it before the next execution | Host reads the expected bytes | Immediate output collection works in this probe. |
| Set `x = 42`, then read state and the output in the next execution | `x` remains; the file is gone | Runtime state persists, but writable files do not. |
| Restore a snapshot taken before staging | `x` disappears, output is empty, staged input remains | Restoring the runtime alone does not provide complete call cleanup. |
| Raise `ValueError`, then execute another program | Exit 1 with guest diagnostic, then successful execution | An ordinary guest exception does not require replacing this VM. |

The matched 0.4.0 stack had the same file deletion and read-only-input behavior. It also discarded the ordinary Python global between consecutive executions. Downgrading to the earlier measured stack therefore does not repair the filesystem prerequisite.

## Why it happens

The released [Wasm `run_impl`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/wasm_sandbox/src/lib.rs) calls `CapFs.prepare_for_run` before entering the guest. In [the released `CapFs`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/hyperlight_sandbox/src/cap_fs.rs), that method calls `clear_output_files`. The generic [Rust `Sandbox.restore`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/hyperlight_sandbox/src/lib.rs) also calls `prepare_for_run`. The deletion is intentional upstream behavior, not an adapter bug or a path-resolution error.

The Python SDK provides output-directory configuration and output discovery, but no opt-in policy that preserves the directory across executions. Direct host writes would also bypass the cached quota counters unless the preserved-mode execution boundary reconciles them. The existing `resynchronize_output` scan is relevant: it accounts for host-side changes, checks individual sizes, refuses symlinks, and bounds traversal. Simply removing the clear is insufficient.

## Proposed upstream change

Add an explicit writable-file lifetime policy, preserving the existing clear-before-execution default. The adapter needs an opt-in mode with the following behavior:

1. A host stages a file under `/output` before execution; the guest can read, modify and delete it during that execution. A guest-created file remains available to the next execution and to host collection between executions.
2. Before entering the guest in preserved mode, reconcile quota accounting against the actual writable tree. Enforce per-file size, aggregate size and file count, including host-staged files and changes to existing files. Over-limit or unsafe entries refuse execution without silently deleting staged content.
3. Keep explicit clear/reset separate from preparation for execution. Restore must still clear writable files and handles in this mode; selecting preservation must not accidentally make restore preserve prior-call outputs. An adapter prepares its configured storage base again after clearing and removes host-managed input staging separately.
4. Expose the policy through the Rust builder, Python native binding, `Sandbox`, `SandboxEnvironment`, and their configuration forwarding tests. Do not silently ignore the option when a native wheel cannot support it. Keep the dependency set pinned when validating the adapter.
5. Add a real Python-guest test for staged input, guest mutation/deletion, cross-execution file persistence, immediate collection and explicit restore. Preserve tests for the existing default and add quota tests covering external staging, overwrite, deletion, unsafe entries and recovery after a rejected execution.

The SDK should name the policy; the exact public spelling remains an upstream design choice. An independent file-enabled adapter must additionally run the suite's confinement, reach, storage-base and disposal conformance checks. Persistence alone does not establish those guarantees.

## Alternatives and remaining work

Using `/input` directly fails the guest-writable requirement. Copying inputs from a hidden staging area in a Python prelude and replaying prior outputs would add a second filesystem lifecycle to the adapter, depend on the guest's mutable Python environment, and still require quota and cleanup accounting. A private patched native wheel would prevent validating an installable published dependency set. These alternatives are not adopted by this record.

The narrow `RUN_CODE`/`SNAPSHOT` package can be built independently if file channels are explicitly deferred. It must withhold file capabilities, reject file-enabled specs, describe the guest truthfully and retain the process deadline and cleanup requirements. Native host tools remain separate under #369 in either order.

These probes do not establish KVM or MSHV support, arbitrary guest compatibility, egress enforcement, native-hang termination, cancellation cleanup, process-tree cleanup, bounded diagnostic output, file confinement or full CodeAct integration. Those remain adapter acceptance work. No production backend declaration or release configuration is changed here.
