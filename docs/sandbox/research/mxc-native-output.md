# MXC native output boundary

> Proposal for [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668): preserve bounded stdout/stderr bytes and keep native completion independent of guest output. Extending the removable bundle into the runtime and embedded kernel is pending a host-maintainer decision. No implementation or runtime qualification is claimed here.

## Source findings

The experiment pins MXC v0.9.0 at `86fb3d2abaf9c431556692037bff881830b543a5` and `hyperlight-unikraft` 0.14.1. The published crate records source commit `2b4790d1626e2645dd1f00469311424be8cb2f94`; that source pins its Unikraft submodule to `195cf5b65e52d9ca3b75b70949ee736a5eb2cad1`. These findings describe those revisions, not newer upstream releases.

Three boundaries lose information or lack a limit:

- Unikraft's [`init_posix_tty`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/lib/posix-tty/tty.c) initializes stderr with `uk_sys_dup2(1, 2)` and supplies the same output file for `/dev/stdout` and `/dev/stderr`. The host receives no original stream identity.
- [`hl_call_host_print`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/hcall.c) clamps the message length to 4096. [`hyperlight_console_out`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/console.c) then returns the original length on callback success. This explains the previously observed silent truncation; retrying the same operation from Python cannot recover a suffix reported as written.
- Rust's [`GuestConfig::register`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/src/lib.rs) accepts `HostPrint` as a `String`, prints it to host stdout, and appends it to an unbounded `Arc<Mutex<String>>`. A parent pipe limit does not bound this accumulator. A string-only callback cannot provide the required arbitrary-byte transport.

## Proposed boundary

Keep the public suite integration beneath MXC. Add separately pinned, reversible patches for the underlying Rust runtime and Unikraft kernel, with a host-controlled experimental feature and an explicitly different runtime profile. Do not edit the Cargo registry cache or replace historical runtime evidence.

Give the guest distinct stdout and stderr file objects so writes, duplicated descriptors and `/dev/stdout` or `/dev/stderr` preserve the destination selected by the program. Intentional program redirection still takes effect. Send bounded raw-byte chunks through a dedicated native callback with explicit stream identity. Keep kernel diagnostics separate. Validate the chunk length and stream value on the host; reject malformed input without using guest bytes as host completion records.

Bound captured bytes in the Rust callback before appending. Do not print captured guest data through a blocking host stdout path during execution. The proposed initial experiment limit is one MiB per stream. On overflow, latch a native failure, stop execution, retire the session and refuse checkpoint publication. Exact-limit output succeeds only if execution also completes normally. An overflow outcome must survive a guest that catches a write error. Timeouts, cancellation and owner loss remain independent termination paths.

Export stdout and stderr as bytes into host-owned bounded result files. Publish a small native control record separately, only after execution and capture succeed. Parse that record with a strict schema and size limit. The host must not infer success from markers, JSON, filenames or other content printed by the guest. No total ordering between concurrent stdout and stderr is promised; byte order within each stream is preserved.

## Compatibility and removal

Changing the embedded kernel changes the runtime's snapshot key, which [`build.rs`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/build.rs) derives from kernel bytes and the snapshot contract. Build a fresh startup snapshot for this profile. Existing session checkpoints remain bound to their original runtime; do not silently restore them with the new kernel or replace them with fresh state.

Build the patched kernel on a GitHub Linux runner from pinned source and retain its hash and build identity. Use the same kernel artifact for separate Windows/WHP and Linux/KVM tests. A kernel build or one platform's pass is not evidence for the other platform.

Keep temporary runtime access behind the existing `probe/backend.rs` wrapper. Replacement requires an equivalent upstream API and rerunning the same behavioral tests before removing the patches. Source removal does not migrate checkpoints or revoke existing executables.

## Qualification plan

Exercise zero-length output; 4095, 4096 and 4097-byte writes; the exact stream limit and one byte beyond; embedded NUL and all byte values; Unicode spanning chunk boundaries; mixed `write` and `writev`; repeated and concurrent writes; descriptor duplication/redirection; and guest text resembling native completion. Verify independent stdout/stderr byte hashes and no silent truncation.

For overflow, malformed native control, guest failure, timeout, cancellation and owner death, verify no successful publication, bounded retained output, native termination and refusal to reuse the failed session. Repeat recovery and lost-acknowledgment tests with the new runtime identity. Retain candidate-specific evidence and leave #1668 open until both platform qualifications pass.

## Why upstream has this shape

History checked on 2026-10-03 distinguishes a console design from the stronger execution contract we need. This is a source and public-discussion audit, not a statement of maintainer intent where none was published.

**Merged stdout/stderr is inherited terminal behavior.** [Unikraft PR #1226](https://github.com/unikraft/unikraft/pull/1226) introduces ordinary file objects for the system console and describes the serial option as preserving the earlier console behavior. Its [stdio initialization commit](https://github.com/unikraft/unikraft/commit/62e8e3983b569c1f71cb4d13345b0fc9fca19ab6) duplicates stdout into stderr. Both writing to one terminal is reasonable console behavior; separate capture requires choosing distinct destinations before that merge. It is not a Hyperlight-specific prohibition on separate streams. [The console-interface work](https://github.com/unikraft/unikraft/pull/1464) also explicitly removed the largely unused distinction between kernel/debug printing. Changing a shared console globally would therefore affect more than Python output.

**The small callback has a defensible boot and performance role.** The [HostPrint introduction](https://github.com/unikraft/unikraft/commit/65a15925fd551aadc9c6fd89bf5b179e442d3032) replaces one VM exit per byte with one per buffer. It uses a fixed stack buffer and preserves a debug-port fallback before host calls are available. Later [host-call buffer work](https://github.com/unikraft/unikraft/commit/b632b9e3de5596511f30c5ef176edd3e98405dbd) explicitly keeps boot-time queries on small stack buffers while allocating generic transport buffers after allocator initialization. This supports retaining an allocation-free early console; it does not justify acknowledging bytes that were never sent. Classifying the silent truncation as a bug is our engineering assessment, not an upstream maintainer verdict found in discussion.

**Capture was added as convenience, without an output-quota policy.** The [capture commit](https://github.com/hyperlight-dev/hyperlight-unikraft/commit/6be6da5465a195d091e4f16234b7d777f006821e) adds the string accumulator and `drain_output()` alongside live printing. The commit contains no rationale for leaving total accumulation unbounded. A console streaming sink and a bounded execution-result collector have different needs; a reusable upstream API could expose a configurable sink and limits without imposing this suite's one-MiB proposal on all users.

**MXC documents process-level console capture.** Its [pinned Hyperlight backend](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) says output goes through HostPrint to the executor process's stdout, leaving the structured stdout/stderr fields empty. It drains the duplicate runtime buffer after execution. The observed limitation follows that integration model. [MXC PR #349](https://github.com/microsoft/mxc/pull/349) fixes terminal chunk presentation, not guest stream identity or native truncation.

**Structured results already have a separate upstream direction.** Current `hyperlight-unikraft` main at `3df47f64f99229e3cebef07b22ba948c69e1398c` declares version 0.17.0. Its [function-call API](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/3df47f64f99229e3cebef07b22ba948c69e1398c/docs/calls.md) returns structured values separately from printed output, with documented 64-KiB guest-result and host-function message limits. This newer API is absent from our pinned 0.14.1 experiment. Even the pinned runtime already signals execution completion through a native `CallDone` event rather than stdout parsing. Neither mechanism provides arbitrary stdout/stderr capture, and guest return values remain untrusted data rather than proof of durable host publication.

The current main's [Rust callback](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/3df47f64f99229e3cebef07b22ba948c69e1398c/src/lib.rs) still appends HostPrint text without a total limit, and its pinned [kernel callback](https://github.com/unikraft/unikraft/blob/bd441efa1f585ef7089cee8900741a3a8ac48374/plat/hyperlight/hcall.c) still has the 4096-byte clamp. Upgrading alone does not resolve #1668. The kernel console also carries an explicit TODO to move kernel diagnostics out of captured guest output. Public issues and relevant PR histories yielded no explicit rejection of bounded separate streams; absence of such a discussion does not establish whether maintainers would accept a particular API.

## Revised recommendation before implementation

Do not treat the three-layer patch as the only possible solution. Preserve the early console and its existing default behavior. Separate the concerns:

1. Prove and fix silent console truncation with a small regression and chunked or correctly reported writes; keep early-boot constraints intact.
2. Propose an opt-in bounded byte-output sink with explicit overflow semantics. Guest output must not block a host callback indefinitely, and a guest catching an I/O error must not clear a latched overflow.
3. Compare a dedicated guest output device against driver-level descriptor redirection into separate pipes or files. Redirection may avoid changing the kernel, but must qualify native-extension writes, descendants, concurrency, bounded storage/draining, and persistent descriptor state; replacing only Python's `sys.stdout` is insufficient. The open upstream [x86_64 Bash pipe failure](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/124) is a reason to test that alternative, not assume it works.
4. Evaluate the newer structured result API independently for result transport. It can reduce custom transport work but cannot replace stdout/stderr fidelity or the host's checkpoint publication protocol.

A kernel patch remains a candidate after those comparisons, with new snapshots and both-platform qualification required. No upstream issue/comment/PR was sent during this investigation, and the pending extension decision has not been treated as approved.
