# Upstream Unikraft output qualification

This experiment tracks [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948). It compares upstream commit `cf458a07ed847f06d78284998a5beb13dc9fb426` with the existing MXC native-output contract. It does not modify the qualified MXC profiles or install an upstream runtime into a persistent store.

The workflow builds one Python rootfs from the pinned upstream driver and passes its exact bytes to both native jobs. Its build report records the CPIO hash and observed Docker image identities. The Dockerfile still installs unversioned Debian packages; a later build is not promised to produce identical bytes. The embedded kernel and Cargo dependency lock come from the pinned upstream commit.

Each native job checks source and rootfs identity, runs five upstream collector tests, three upstream native output-limit tests, the upstream echo test, and seven independent comparison tests. Missing or ignored tests refuse qualification. The report records kernel, rootfs, dependency lock, probe and native comparison executable hashes. Logs and reports are uploaded even on failure. Guest networking remains disabled by the upstream builder default.

The comparison tests intentionally establish whether upstream matches the existing contract. A successful experiment may establish a mismatch: stderr consumes the combined output budget, draining combined output clears stderr metadata, arbitrary binary output is rejected, and reopening stderr fails. The diagnostic probes assert these observed gaps rather than treating them as supported behavior. Such a result is recorded as `measured` with `defer-overlay-removal`, never as a qualified MXC integration. The guest-control-text probe checks that text is returned as data; it does not exercise our durable publication protocol.

Reproduce by dispatching the **Upstream Unikraft output qualification** workflow at the reviewed branch. The workflow supplies a fresh pinned checkout, Rust 1.98.0, the matching rootfs and a build report from that same run and attempt. Its native step runs `python scripts/experiments/unikraft_output/qualify.py --source upstream --output evidence`; this runner intentionally requires the producing workflow identity. Existing test files or modified tracked upstream source are refused. The temporary integration test is removed after execution.

The [research record](../../../docs/sandbox/research/mxc-native-output.md) owns the adoption decision. Old-checkpoint restore, MXC durable publication/replay and the complete timeout/cancellation/owner-loss matrix require an integrated migration candidate; the standalone upstream tests do not establish them.

## Retained measurements

[Run 38652752789](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789) tested source `9c61b9e8520dfa839596e07dcf29e293108125f4`. The [Linux](linux-result.json) and [Windows](windows-result.json) reports match on source, kernel, rootfs, dependency lock, probe and observations. Each completed nine upstream controls and seven comparison/diagnostic controls. The result is **defer overlay removal**: binary data is rejected, reopened stderr returns EINVAL in the tested image, and combined capture is not an independent stdout budget.

The initial [Linux](linux-initial.json) and [Windows](windows-initial.json) reports preserve the two failed comparisons from [run 38651934008](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008). Subsequent diagnostic tests characterize those failures; they do not turn unsupported behavior into a qualification pass. Later report/documentation commits and the switch to manual workflow dispatch do not change the tested native harness.
