# Descriptor output experiment

[descriptor_probe.py](descriptor_probe.py) tests whether guest descriptor redirection can satisfy [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) without changing the embedded kernel. It uses the existing pinned MXC session patch; `execute-owned` executes once under the private owner pipe without exporting a checkpoint. `call-owned` still tests checkpoint export. The descriptor experiment changes no shipped package or upstream runtime API.

## Result

Independent Windows/WHP and Linux/KVM measurements establish **no-go for guest descriptor redirection alone as a host-enforced output boundary**. [Windows evidence](windows-descriptor-result.json) and [Linux evidence](linux-descriptor-result.json) record individual outcomes and executable identities. [GitHub run 37136213341](https://github.com/sokolaidev/maf-extensions/actions/runs/37136213341) passed the complete workflow at source `5b2436f57c672889f50277d30a2083a69735b0c3`, including the existing native recovery, atomic publication and owner-liveness controls. Both retained output measurements use that source. A subsequent review fix moves descriptor and console writes outside assertions so Python optimization cannot remove them; these retained native measurements predate that fix. An ordinary PR check does not execute the opt-in runtime probe.

| Control | Windows/WHP and Linux/KVM observation |
|---|---|
| Console writes of 4095 / 4096 / 4097 bytes | 4095 / 4096 / 4096 bytes arrive, while all three writes and native executions report success |
| Separate pipes with binary and Unicode, concurrent writers, duplicated descriptors, writev and native C write | Exact expected hashes; 20,314 bytes in each stream |
| Exact one-MiB collector limit | Complete retained output |
| One byte beyond the collector limit | Guest collector retains one MiB and flags overflow; native execution still reports success |
| Reopen `/dev/stdout` after redirection | Bytes bypass both pipe collectors and reach the original native console |
| Guest file-size limit | `RLIMIT_FSIZE` refuses resource 1 with EINVAL |
| Native deadline / explicit owner cancellation while writing to pipes | Exit 1 with TimedOut / exit 74; neither produces native success |
| Open redirected file / buffered pipe across process restart | Both preserve the expected before/after binary bytes and descriptor state |

The pipe measurements are observations from fixed cooperative guest programs. The collector deliberately reports its overflow flag rather than raising; this demonstrates that the existing native path does not enforce that flag. A cooperative wrapper could raise, but guest code can bypass or replace that wrapper. Their hashes and counters do not create host authority, enforce a native memory quota, or prevent hostile guest code from replacing a reader or bypassing it. The supervisor independently bounds its retained process output and wall time. Its fallback kill is an inconclusive runtime measurement, never a passing deadline result. The evidence validator requires native completion, expected byte hashes and all counterexamples; tests reject missing controls, corrupted hashes and supervisor fallback.

The checkpoint test preserves open file and buffered pipe descriptors, not actively draining reader threads or arbitrary descendants. No general host file-export mechanism or safe artifact collection is established. The filesystem alternative would also require a quota outside guest control. The experiment fails while querying the file-size limit, before setting it or writing the test file; the pinned kernel's `uk_sys_prlimit64` resource switch also excludes `RLIMIT_FSIZE` for setting and querying. Catching that refusal cannot provide a quota. #1668 remains open.

Local verification passed 13,196 tests with 751 skipped, repository lint/format and package/root type checks, documentation checks, two Rust tests, Clippy with warnings denied, and real helper help/invalid-argument controls. The eight evidence-validator regressions are included in that historical count. Subsequent subprocess regressions check complete descriptor payloads with and without Python optimization and the three console boundary writes under `-O`; Windows substitutes POSIX write calls with `os.write` adapters, while Linux executes the native calls. These checks do not establish arbitrary descendant capture, reader-thread snapshot safety, native output quotas or a production backend.

## Reproduce

Build the helper using the [existing patch instructions](README.md#apply-and-reproduce) and prepare a matching startup snapshot. Use a new evidence directory outside the repository:

```text
python scripts/experiments/mxc_session_patch/descriptor_probe.py --helper <native-helper> --startup <startup-snapshot> --state-dir <new-evidence-directory>
```

The existing Tests workflow runs the same probe on GitHub Linux/KVM when dispatched with `mxc_recovery=true`. It retains JSON and bounded diagnostic streams, not snapshot directories. The probe's final assessment distinguishes successful reproduction of a limitation from successful qualification of the requested contract.

## Minimal console reproducer and proposed fix

Execute this fixed guest code with `execute-owned` and a matching startup snapshot, retaining native stdout separately from the native control file:

```python
import os

written = os.write(1, b"X" * 4097)
if written != 4097:
    raise RuntimeError(f"console write returned {written}, expected 4097")
```

The unmodified pinned console emits 4096 `X` bytes and the helper writes `{"executed": true}`. The 4095/4096 positive controls exclude a generic failure to execute or collect output. This reproducer is also run automatically by the descriptor probe.

The proposed upstream correction is to preserve the allocation-free early console but stop acknowledging unsent bytes: loop over bounded chunks or propagate a truthful short write/error through the console layers. A chunking implementation must handle UTF-8 boundaries because the current HostPrint parameter is a string; merely slicing every 4096 bytes risks splitting a code point. Arbitrary binary output and distinct streams require a byte-oriented interface or separately qualified transport. This is a proposed fix, not an implemented or tested kernel patch.

The [source/history research](../../../docs/sandbox/research/mxc-native-output.md) explains why the existing console is shared, identifies the newer structured result channel, and compares the remaining implementation choices. The subsequent bounded-capture request is reported in [hyperlight-unikraft #140](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140). No upstream PR has been submitted.

## Bounded native capture overlay

The [accepted design](../../../docs/sandbox/backends/mxc.md#bounded-console-capture) sets a **host-configurable 1 MiB combined console budget per call, with truncation and continued execution**. `output-runtime.patch` bounds the Rust HostPrint collector before appending and disables automatic stdout mirroring when bounded capture is selected. The callback retains a valid UTF-8 prefix; if the next character would cross the limit, it omits that character and all subsequent output. Empty output at a zero limit and exact-limit output without omission are not truncated. Omitted bytes are counted with an explicit saturation flag. These counters describe messages received by HostPrint; they cannot detect bytes the kernel already discarded.

`output-session.patch` layers over the unchanged session-preview patch and adds the opt-in `maf-output-preview` feature. Startup/resume has a bounded interval of its own. Execution starts a fresh budget, and the host receives capture metadata separately from guest text. Truncation does not change the session commit barrier, guest-error behavior, deadline or owner cancellation. The `call-bounded` helper writes a bounded text file and a small native control record to host-owned paths. The host validates their sizes and metadata before saving a base64-encoded console payload and truncation metadata in the existing checkpoint/result transaction. The experimental store's independent two-MiB serialized-result limit still applies; setting a larger output budget does not raise that storage limit.

The overlay preserves the default legacy console path for the earlier probes and leaves kernel bytes unchanged. It does not provide arbitrary binary fidelity, separate stdout/stderr, a fix for silent 4096-byte console truncation, process-wide memory containment or safe general file export. #1668 remains open. A host runtime profile includes the helper identity and output policy; this work does not authorize silently changing an existing session's profile.

### Apply and remove the overlay

Start with the existing session patch applied to its pinned MXC checkout, and a separate clean `hyperlight-unikraft` checkout at `2b4790d1626e2645dd1f00469311424be8cb2f94`. The overlay tool checks both revisions, patch checksums and exact affected-file contents before modifying either checkout. Keep the registry cache unchanged. The generated manifest overrides only `hyperlight-unikraft` with the explicitly selected source; all other dependency versions remain locked.

```text
python scripts/experiments/mxc_session_patch/output_patch.py apply --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/output_patch.py configure --source <mxc-checkout> --runtime <runtime-checkout> --build-dir <new-build-directory>
cargo build --locked --manifest-path <new-build-directory>/Cargo.toml
python scripts/experiments/mxc_session_patch/bounded_probe.py --helper <native-helper> --startup <startup-snapshot> --state-dir <new-evidence-directory>
```

The generated helper enables bounded-output support; its `call-bounded` command defaults to 1,048,576 retained bytes and accepts an optional final byte-limit argument. The durable host harness selects this mode with `--bounded-output`, with an optional `--output-limit`. The original commands preserve the historical probes' behavior.

Stop sessions using the experimental executable before replacing it. Remove this overlay before removing the prerequisite session patch:

```text
python scripts/experiments/mxc_session_patch/output_patch.py remove --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/output_patch.py check --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
```

An upstream replacement must pass the same retained-output, overflow, recovery and metadata validation checks before removing the overlay and selecting the new API behind `probe/backend.rs`. Source removal alone neither revokes built executables nor migrates existing stores.

### Qualification

`bounded_probe.py` exercises the default limit, exact-limit output, overflow followed by successful state recovery, a custom small limit, zero retention, UTF-8 boundaries, reopening `/dev/stdout`, a lost acknowledgment after publishing a truncated result, guest failure and an output-producing timeout. It rejects supervisor fallback as an inconclusive result. Native collector unit tests additionally exercise sustained over-limit input, metadata-counter saturation, the legacy default, bounded/legacy small-message growth, unlocked mirroring, and capture recovery after a mirror panic. Linux measurements run through the opt-in GitHub KVM job; separate Windows/WHP evidence is required. The retained reports below record successful qualification of this bounded text-capture scope.

The current [Windows/WHP report](windows-bounded-result.json) and [Linux/KVM report](linux-bounded-result.json) passed at source `9e10213d8ca566848ec8acc9b6ede1bc185514e8`. Retained output hashes and counters match between platforms and match the earlier candidate. [GitHub run 37159683759](https://github.com/sokolaidev/maf-extensions/actions/runs/37159683759) passed the complete workflow, including native recovery, publication, owner-liveness, descriptor controls, bounded-output qualification and overlay removal. Artifact `11286634373` has SHA-256 `5cebe0dd8b6354f51641624b645634921aa1381d0fb95c91e874e871135627c1`. The first Windows attempt on this revision failed during the overflow case when the native process could not allocate 1 GiB; it is not counted as a qualification pass. The retry passed after the full local suite ended, without changing the code or host settings. The earlier candidate at `c279bbe0` also passed both platforms, including [GitHub run 37152285593](https://github.com/sokolaidev/maf-extensions/actions/runs/37152285593).

At `9e10213d`, focused validation passed 52 Python tests, five native collector tests, two helper tests and Clippy with warnings denied. Repository lint/format, package/root type checks, Markdown and authenticated tracker checks passed. GitHub offline suites passed 13,896 tests with 593 skipped, alongside packaging and published-core compatibility checks. The local full suite reported 13,694 passed, 794 skipped and one failure in the unchanged WSLC `-W error` import test; that test and its complete four-test packaging file passed on isolated reruns without changes. The original local full run is not a green gate. This qualifies the tested restored-session profile; independent cold-boot output stress, arbitrary binary fidelity, separate streams and process-wide memory limits remain outside the evidence.

The retained runtime reports above predate the collector growth and mirroring review fixes. The revised collector grows bounded capacity geometrically up to the configured limit and preserves the legacy string growth policy. Legacy mirroring runs without the capture lock so a stdout panic cannot poison capture. Review validation is recorded in [#1693](https://github.com/sokolaidev/maf-extensions/pull/1693); the earlier reports do not claim a native run of the revised collector.
