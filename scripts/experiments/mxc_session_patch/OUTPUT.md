# Descriptor output experiment

[descriptor_probe.py](descriptor_probe.py) tests whether guest descriptor redirection can satisfy [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) without changing the embedded kernel. It uses the existing pinned MXC session patch; `execute-owned` executes once under the private owner pipe without exporting a checkpoint. `call-owned` still tests checkpoint export. No shipped package or upstream runtime API changes.

## Preliminary result

Windows/WHP establishes **no-go for guest descriptor redirection alone as a host-enforced output boundary**. [windows-descriptor-result.json](windows-descriptor-result.json) records individual outcomes and executable identity. Linux/KVM qualification is pending the explicitly dispatched workflow; an ordinary PR check does not execute this probe.

| Control | Windows observation |
|---|---|
| Console writes of 4095 / 4096 / 4097 bytes | 4095 / 4096 / 4096 bytes arrive, while all three writes and native executions report success |
| Separate pipes with binary and Unicode, concurrent writers, duplicated descriptors, writev and native C write | Exact expected hashes; 20,314 bytes in each stream |
| Exact one-MiB collector limit | Complete retained output |
| One byte beyond the collector limit | Guest collector retains one MiB and flags overflow; native execution still reports success |
| Reopen `/dev/stdout` after redirection | Bytes bypass both pipe collectors and reach the original native console |
| Guest file-size limit | `RLIMIT_FSIZE` refuses resource 1 with EINVAL |
| Native deadline / explicit owner cancellation while writing to pipes | Exit 1 with TimedOut / exit 74; neither produces native success |
| Open redirected file / buffered pipe across process restart | Both preserve the expected before/after binary bytes and descriptor state |

The pipe measurements are observations from fixed cooperative guest programs. Their hashes and counters do not create host authority, enforce a native memory quota, or prevent hostile guest code from replacing a reader or bypassing it. The supervisor independently bounds its retained process output and wall time. Its fallback kill is an inconclusive runtime measurement, never a passing deadline result. The evidence validator requires native completion, expected byte hashes and all counterexamples; tests reject missing controls, corrupted hashes and supervisor fallback.

The checkpoint test preserves open file and buffered pipe descriptors, not actively draining reader threads or arbitrary descendants. No general host file-export mechanism or safe artifact collection is established. The filesystem alternative would also require a quota outside guest control; catching the unsupported resource limit cannot provide one. #1668 remains open.

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

assert os.write(1, b"X" * 4097) == 4097
```

The unmodified pinned console emits 4096 `X` bytes and the helper writes `{"executed": true}`. The 4095/4096 positive controls exclude a generic failure to execute or collect output. This reproducer is also run automatically by the descriptor probe.

The proposed upstream correction is to preserve the allocation-free early console but stop acknowledging unsent bytes: loop over bounded chunks or propagate a truthful short write/error through the console layers. A chunking implementation must handle UTF-8 boundaries because the current HostPrint parameter is a string; merely slicing every 4096 bytes risks splitting a code point. Arbitrary binary output and distinct streams require a byte-oriented interface or separately qualified transport. This is a proposed fix, not an implemented or tested kernel patch.

The [source/history research](../../../docs/sandbox/research/mxc-native-output.md) explains why the existing console is shared, identifies the newer structured result channel, and compares the remaining implementation choices. No upstream issue, comment or PR is submitted by this experiment.
