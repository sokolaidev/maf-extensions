# MXC native output boundary

> Research for [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668): preserve bounded stdout/stderr bytes and keep native completion independent of guest output. The original proposal and investigation below describe the 0.9 experiment. The separate MXC 1.0 byte-stream profile is experimentally qualified on Linux/KVM and Windows/WHP; the final section records the candidate and validation boundaries.
> The later accepted bounded-console policy is recorded in the [MXC design](../backends/mxc.md#bounded-console-capture).

## Source findings

The experiment pins MXC v0.9.0 at `86fb3d2abaf9c431556692037bff881830b543a5` and `hyperlight-unikraft` 0.14.1. The published crate records source commit `2b4790d1626e2645dd1f00469311424be8cb2f94`; that source pins its Unikraft submodule to `195cf5b65e52d9ca3b75b70949ee736a5eb2cad1`. These findings describe those revisions, not newer upstream releases.

Three boundaries lose information or lack a limit:

- Unikraft's [`init_posix_tty`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/lib/posix-tty/tty.c) initializes stderr with `uk_sys_dup2(1, 2)` and supplies the same output file for `/dev/stdout` and `/dev/stderr`. The host receives no original stream identity.
- [`hl_call_host_print`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/hcall.c) clamps the message length to 4096. [`hyperlight_console_out`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/console.c) then returns the original length on callback success. This explains the previously observed silent truncation; retrying the same operation from Python cannot recover a suffix reported as written.
- Rust's [`GuestConfig::register`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/src/lib.rs) accepts `HostPrint` as a `String`, prints it to host stdout, and appends it to an unbounded `Arc<Mutex<String>>`. A parent pipe limit does not bound this accumulator. A string-only callback cannot provide the required arbitrary-byte transport.

## Original proposed boundary

Keep the public suite integration beneath MXC. Add separately pinned, reversible patches for the underlying Rust runtime and Unikraft kernel, with a host-controlled experimental feature and an explicitly different runtime profile. Do not edit the Cargo registry cache or replace historical runtime evidence.

Give the guest distinct stdout and stderr file objects so writes, duplicated descriptors and `/dev/stdout` or `/dev/stderr` preserve the destination selected by the program. Intentional program redirection still takes effect. Send bounded raw-byte chunks through a dedicated native callback with explicit stream identity. Keep kernel diagnostics separate. Validate the chunk length and stream value on the host; reject malformed input without using guest bytes as host completion records.

Bound captured bytes in the Rust callback before appending. Do not print captured guest data through a blocking host stdout path during execution. The proposed initial experiment limit is one MiB per stream. On overflow, latch a native failure, stop execution, retire the session and refuse checkpoint publication. Exact-limit output succeeds only if execution also completes normally. An overflow outcome must survive a guest that catches a write error. Timeouts, cancellation and owner loss remain independent termination paths.

Export stdout and stderr as bytes into host-owned bounded result files. Publish a small native control record separately, only after execution and capture succeed. Parse that record with a strict schema and size limit. The host must not infer success from markers, JSON, filenames or other content printed by the guest. No total ordering between concurrent stdout and stderr is promised; byte order within each stream is preserved.

## Compatibility and removal

Changing the embedded kernel changes the runtime's snapshot key, which [`build.rs`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/build.rs) derives from kernel bytes and the snapshot contract. Build a fresh startup snapshot for this profile. Existing session checkpoints remain bound to their original runtime; do not silently restore them with the new kernel or replace them with fresh state.

Build the patched kernel on a GitHub Linux runner from pinned source and retain its hash and build identity. Use the same kernel artifact for separate Windows/WHP and Linux/KVM tests. A kernel build or one platform's pass is not evidence for the other platform.

Keep temporary runtime access behind the existing `probe/backend.rs` wrapper. Replacement requires an equivalent upstream API and rerunning the same behavioral tests before removing the patches. Source removal does not migrate checkpoints or revoke existing executables.

## Original qualification plan

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

## MXC 1.0 implementation and qualification (2026-10-08)

Merged [#1784](https://github.com/sokolaidev/maf-extensions/pull/1784) supplies the qualified MXC 1.0 session, bounded-console and storage baseline. The new experiment extends that baseline with a separately removable kernel layer in [`mxc_streams_patch`](../../../scripts/experiments/mxc_streams_patch/). It pins MXC `7bf210247986cb73b1b314df60c2f1109c479c0b`, runtime `8f636e00cdf29e6c33ba0f578482c595d7827cf6`, and Unikraft `978c8a7d3ac33aa06cef870aead0182c89974002`. The patch metadata also pins the elfloader and libelf submodules and checks every affected source file before applying or removing the layer.

The opt-in kernel configuration gives stdout and stderr separate file objects, including their `/dev` entries. Each write carries a stream ID and raw bytes through a dedicated `MafWriteBytes` callback in chunks of at most 4096 bytes. This avoids the serial console's string conversion, newline rewriting and fixed-message truncation. Descriptor duplication and program redirection still select the destination file object. Kernel diagnostics retain the existing console. The profile also routes the pinned runtime's older devfs implementation through the same byte file objects and registers its missing stderr device. Both paths are covered by the native results below.

The GitHub Linux build retains the kernel hash, source identities, builder image identity and installed toolchain package list. The upstream builder uses a tagged base image and unversioned package installation, so these records identify the observed build rather than promise reproducible bytes across future builds. Both native host qualifications must consume the same resulting kernel artifact. A successful build is reported as `built-not-qualified`.

The host maintainer accepted one MiB per stream on 2026-10-08: retain a byte prefix, report omitted bytes with saturating counters, and let execution continue. Each stream has its own budget, reset by the host between calls. A successful guest write still reports its full length when retention truncates. Malformed transport remains a latched failure that retires the session and refuses checkpoint publication. This supersedes the earlier fail-and-retire overflow proposal for this new profile.

The new profile explicitly reserves three MiB for a result envelope, covering two full one-MiB streams encoded as base64 plus bounded metadata. The format-4 result allowance now permits this explicit configuration; its two-MiB default and the older store remain unchanged. Admission reserves this allowance before native execution, and scratch accounting includes both byte files and separate bounded diagnostics. Existing console capture, result envelopes and historical evidence remain unchanged. The runtime callback, native control validation and probes are implemented and experimentally qualified. [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786) delivers #1668; the production adapter and general bounded file/artifact plane remain separate work.

The first kernel build passed on a GitHub Linux runner in [run 37769914334](https://github.com/sokolaidev/maf-extensions/actions/runs/37769914334), attempt 1, from candidate `a92b193d6103a03047229d43d5e1a391132ea250`. The retained [build report](../../../scripts/experiments/mxc_streams_patch/kernel-build-result.json) records an 873,448-byte x86-64 ELF with SHA-256 `b25f0b8143f41a98fbb70a1cc0ee2fcab035b91b6f36b8e99f55b66b8f2d746d` and successful overlay removal. That first artifact was compilation evidence only and predates the dedicated host callback and older-devfs correction. It must not replace the kernel in an existing runtime profile.

### Qualified byte-stream candidate

[GitHub run 37776716075](https://github.com/sokolaidev/maf-extensions/actions/runs/37776716075), attempt 1, qualified candidate `57f5c3aae00bab7970bdb5dd81fea7df82229561` independently on Linux/KVM and Windows/WHP. The retained [Linux report](../../../scripts/experiments/mxc_streams_patch/linux-result.json) and [Windows report](../../../scripts/experiments/mxc_streams_patch/windows-result.json) identify the helper, dependency lock, source-built kernel, builder identity and Python-state controls. Both hosts consumed kernel SHA-256 `9e638ba3d231adc8288ddda4c344019afd9d9fbd1d6987d28b26eed707a6fe19` and report successful overlay removal. The kernel profile differs from the earlier build-only artifact.

All 23 stream/failure cases matched across hosts: zero output; 4095/4096/4097-byte and one-MiB boundaries; all byte values; Unicode crossing a chunk boundary; writev and native C writes; descriptor duplication/redirection and reopened devices; concurrent stdout/stderr; text resembling control; continued execution after truncation and fresh-process recovery; guest error, timeout, owner loss and cancellation. A 16-MiB flood per stream retained exactly one MiB and counted the remaining 15 MiB. Reopened devices obeyed the same bounds. Rust unit tests independently covered malformed stream frames, sticky failure and counter saturation; Python tests covered strict control validation and pre-launch admission refusal.

The format-4 crash matrix exercised all eight publication/cleanup boundaries with both full byte prefixes and omission counts. Committed retries returned the identical envelope without a helper, including after checkpoint collection. Uncommitted failure preserved the previous checkpoint; quota refusal occurred before native launch; independently reconciled SQLite totals matched persisted accounting. This is same-machine process-crash evidence, not cross-machine fencing, machine power-loss evidence or a production adapter qualification.

After this native run, the only executable adjustment tightened host scratch admission by one KiB to reserve readiness/control headroom explicitly. Its focused boundary test passed; the native kernel, Rust overlays, helper and transport remained unchanged. The reports deliberately retain the actual native-tested commit rather than relabeling it as the final documentation commit. The [experiment README](../../../scripts/experiments/mxc_streams_patch/README.md) describes running and removing the profile.

## Upstream bounded capture qualification (2026-10-15)

[Upstream PR #146](https://github.com/hyperlight-dev/hyperlight-unikraft/pull/146) closed [upstream #140](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140) at commit `cf458a07ed847f06d78284998a5beb13dc9fb426`. [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) tracks downstream qualification. This comparison preserves the older experiments and their evidence; the upstream fix does not retroactively change their pinned runtime.

### Source comparison

| Concern | Upstream at the pinned commit | Consequence for the MXC profile |
|---|---|---|
| Retained output | `output_limit(bytes)` bounds each capture between drains; overflow retains a UTF-8 prefix and continues execution | The original host-accumulation requirement is implemented, but the default remains unlimited |
| Mirroring | `echo_output(false)` is a separate setting | Both settings must be applied on fresh boot and restore |
| Stream identity | `take_output()` contains stdout and stderr together; `take_stderr()` duplicates stderr separately | A stderr flood can exhaust the combined budget before stdout is retained; these are not independent stdout/stderr budgets |
| Drain ordering | `take_output()` clears the stderr text and omission counter | Stderr must be collected first |
| Binary transport | `CapturedOutput.text` is a Rust `String`; callbacks accept strings | It cannot represent the existing arbitrary-byte stream contract |
| Omission counters | `Capture::push` uses ordinary `u64` addition without a saturation flag | Preserve the downstream saturation contract; this is a source finding, not a measured long-running overflow |
| Persistence | The upstream output API does not implement the MXC format-4 result/checkpoint transaction or bounded-export overlay | Session, storage and durable replay support cannot be retired on the strength of this fix |

The upstream snapshot contract is `c4` and its embedded x86-64 kernel has SHA-256 `2d5dc13d1dd13e31b37c98f0ab5413d90b5eed9104e6215e1e06ede559c9f275`. The retained MXC 1.0 runtime at `8f636e00cdf29e6c33ba0f578482c595d7827cf6` used contract `c3`. These source identities differ before attempting any old-checkpoint restore; a migration must keep old profiles available and explicitly qualify refusal/recovery. This comparison does not attribute all intervening kernel and contract changes to PR #146.

The [qualification harness](../../../scripts/experiments/unikraft_output/README.md) runs the exact upstream collector, output-limit, restore and echo tests alongside independent native contract comparisons. It leaves the existing MXC overlay sources and pins unchanged.

### Native measurements and decision

The first [comparison run 38651934008](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008), source `a4d79f147fbf257f6032472fee18d4627948f1b1`, passed all nine upstream controls on both hosts but failed two of six independent comparisons. The unqualified [Linux](../../../scripts/experiments/unikraft_output/linux-initial.json) and [Windows](../../../scripts/experiments/unikraft_output/windows-initial.json) reports remain retained. Binary output produced a host-call decoding error; the combined Unicode/reopened-device probe failed when opening stderr.

The diagnostic [run 38652752789](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789), source `9c61b9e8520dfa839596e07dcf29e293108125f4`, separated the reopened-device cases and captured the failure details. Both [Linux/KVM](../../../scripts/experiments/unikraft_output/linux-result.json) and [Windows/WHP](../../../scripts/experiments/unikraft_output/windows-result.json) completed five upstream collector tests, three upstream native limit/restore tests, one upstream echo test and seven independent comparison/diagnostic tests. The diagnostic tests assert the observed gaps; their successful completion means the behavior was measured, not that MXC adoption passed.

Both hosts used CPIO SHA-256 `8a1439a2085d218500f4ef5cfe42aecd63a233ac01f09db2ad95b9b3eaff52e9`, dependency-lock SHA-256 `f9611836e9490ac971e7e463fd771076005b03b1833d837917f371692c2dbde8`, and the same probe bytes. Their observations match exactly. The reports retain the observed base/rootfs image identities, kernel and executable hashes, Rust version, source commit, run and attempt. This is a standalone upstream Python rootfs, not the MXC rich-agent helper or a production adapter.

| Measured behavior | Linux/KVM and Windows/WHP result |
|---|---|
| Prefix limits, 4095/4096/4097-byte boundaries, four-MiB output, continuation and reset | Passed |
| UTF-8 prefix, reopened stdout, restore and echo controls | Passed |
| Combined drain ordering | Clears both stderr text and its omitted count, as documented upstream |
| Independent stdout budget | Not equivalent: 32 stderr bytes exhaust a 16-byte combined prefix; eight later stdout bytes are omitted, even though stderr is separately retained |
| All 256 byte values | Rejected with `ReadHostFunctionCall` / FlatBuffer function-call decoding error |
| Reopened stderr in this image | `OSError: [Errno 22] Invalid argument: '/dev/stderr'`, native `CallFailed { status: 1 }` |

**Decision: defer overlay removal.** The original configurable text-capture bound is implemented and its controls pass on both hosts. Upstream PR #146 does not replace the qualified independent byte-stream transport. A future adapter may reuse bounded text capture for diagnostics, but must explicitly disable echo, account for both buffers, respect drain ordering and preserve omission-counter semantics. Keep the current session, storage, stream and kernel profiles available.

No migrated MXC helper was qualified here. Durable result/checkpoint publication and replay, old-checkpoint refusal/recovery, and the complete timeout/cancellation/owner-loss matrix remain unverified on this upstream revision. [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) stays open for that adoption work beneath parent #1648. The earlier reports continue to describe only their original candidates.
