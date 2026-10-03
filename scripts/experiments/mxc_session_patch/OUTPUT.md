# Descriptor output experiment

[descriptor_probe.py](descriptor_probe.py) tests whether guest descriptor redirection can satisfy [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) without changing the embedded kernel. It uses the existing pinned MXC session patch; `execute-owned` executes once under the private owner pipe without exporting a checkpoint. `call-owned` still tests checkpoint export. No shipped package or upstream runtime API changes.

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

The [source/history research](../../../docs/sandbox/research/mxc-native-output.md) explains why the existing console is shared, identifies the newer structured result channel, and compares the remaining implementation choices. No upstream issue, comment or PR is submitted by this experiment.
