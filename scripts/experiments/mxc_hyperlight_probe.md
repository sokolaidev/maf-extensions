# MXC Hyperlight spike #1649

Initial Windows evidence for [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649), recorded on 2026-10-02. This is an experiment, not a backend implementation or a completed persistence qualification.

## Reproduction

Download the `mxc-release-binaries.zip` asset from [MXC v0.9.0](https://github.com/microsoft/mxc/releases/tag/v0.9.0), verify its SHA-256 below, and extract the platform executor and adjacent libraries. Run [the probe](mxc_hyperlight_probe.py) with an absolute executor location and a dedicated state directory. `--setup` explicitly downloads and warms the agent image; subsequent `--run` executions reuse that prepared image. Omitting both options performs only help, availability and schema checks.

```text
python scripts/experiments/mxc_hyperlight_probe.py --executor <executor> --state-dir <isolated-directory> --setup --run
```

The probe supplies a small host environment, uses its own image home, and submits fixed programs without mounts or a network configuration. It records stdout, stderr, requests and process exit status separately. Exit zero from the probe means the recording completed; it is not a conformance verdict. The controls deliberately include rejected configuration and a guest exception. The timeouts bound these fixed experiments; this harness does not implement production process-tree supervision or output quotas. Keep the state directory outside Git: runtime files are large and diagnostics can include host locations.

## Artifact identity

| Artifact | Identity |
|---|---|
| MXC source | `86fb3d2abaf9c431556692037bff881830b543a5` (`v0.9.0`) |
| Release archive SHA-256 | `f233b59ade8639d5121615b6ab63d941ddc312b1f008c291a728cad37d84fe74` |
| Windows x64 executor SHA-256 | `819ecd9b08a8dcb2376d489c0352087468bf56800ab059f57d38901d7743049e` |
| Authenticode | Valid Microsoft Corporation signature at verification time |
| Guest rootfs | `ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v0.14.1` |
| Downloaded rootfs layer | `sha256:b7e71eeaaea60deb87456f62bb793ea1503329c130c9b7b8dc9693b998851c4b` |
| Extracted initrd SHA-256 | `8a9b9e383510dea8bb58b3aca72ac4a8d141f163f6a2fdc68af54197c5922e7e` |
| Guest Python | CPython 3.12.14 |

## Observed Windows controls

| Control | Result | Interpretation |
|---|---|---|
| `--help`, `--probe` | Exit 0; `hyperlightAvailable: true` | Executor starts and detects local Hyperlight availability |
| Closed request, experimental schema | Dry-run exit 0 | Request accepted; does not prove network confinement |
| Legacy `network.allowedHosts` | Dry-run exit 1; unknown field, expected `egress` or `ingress` | Legacy allowlist translation cannot be reached through this public request shape |
| Agent setup | Exit 0; downloaded rootfs and created warm snapshot | Runtime installation and initial guest boot succeeded |
| NumPy/pandas | Exit 0; `np.arange(4).sum()` = `6`; dataframe sum = `{'x': 3}` | Rich Python executes through the released Windows binary |
| stdout/stderr markers | Both `guest-stdout` and `guest-stderr` in process stdout; process stderr empty | Independent host pipes do not preserve guest stream identity |
| Controlled `ValueError` | Exit 1; traceback and driver diagnostic in stdout; stderr empty | Guest failure status is distinguishable in this case; guest text and diagnostics are mixed |
| Fresh seed/read in separate executor processes | Seed prints `73`; reader prints `absent` | Fresh-execution control passes; this does not test a reused guest |

## Persistence investigation

The pinned [MXC runner](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) restores `rewind` before every execution after the first. Its guest and rewind fields are private. The CLI exposes no session checkpoint operation; the inspected SDK/FFI entry points do not provide a Hyperlight modified-state capture/restore contract. A persistent helper around the unchanged MXC runner would therefore still lose state.

The lower-level [hyperlight-unikraft 0.14.1 API](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/v0.14.1/src/lib.rs) exposes `AppSandbox.run`, `snapshot_to`, `restore_from` and `SandboxBuilder.from_snapshot_dir`. Its documentation describes capturing the application heap, parked threads and scheduler state at the current boundary. The [Python driver](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/v0.14.1/drivers/hl_py.h) executes source in the existing `__main__` dictionary. These are source-level reasons to try a persistent native helper; they are not measured proof of durable Python recovery.

The native experiment below now exercises this path. A later experiment must coordinate session files and host-controlled atomic commitment; the library's disk snapshot alone does not establish that application contract.

A helper that calls Unikraft directly would bypass MXC's current runner. Treat it as an experimental feasibility probe and identify the MXC API change or alternative integration needed before proposing it as the production adapter. No upstream issue or PR has been published.

## Native state and restart experiment

The [native helper](mxc_native_state/src/main.rs) uses exactly `hyperlight-unikraft` 0.14.1, with Hyperlight host/common 0.17.0 recorded in [Cargo.lock](mxc_native_state/Cargo.lock). The [supervisor](mxc_native_state_probe.py) verifies the recorded rootfs hash, supplies a minimal environment, and requires a new evidence directory. No host mounts or guest network policy are configured. This exercises Unikraft directly, bypassing MXC's runner.

```text
cargo build --locked --manifest-path scripts/experiments/mxc_native_state/Cargo.toml
python scripts/experiments/mxc_native_state_probe.py --helper <built-helper> --initrd <verified-agent-initrd> --state-dir <new-evidence-directory>
```

On Windows, this experiment used Rust 1.98.0 targeting `x86_64-pc-windows-gnu` and portable MinGW GCC 16.2.0 (POSIX/SEH/MSVCRT). Rust and compiler tools were isolated from the user's normal environment. The GCC archive was `x86_64-16.2.0-release-posix-seh-msvcrt-rt_v14-rev1.7z`, SHA-256 `a3cfb25037981ea1cd2f4e5452fa45c700b20fffc311e5d78de90dedd142b73a`. Set that compiler's `bin` directory on the build process's PATH; ordinary Windows MSVC and Linux builds have not been qualified here. The compiler version is pinned in [rust-toolchain.toml](mxc_native_state/rust-toolchain.toml).

| Native control | Measured Windows result |
|---|---|
| Multiple executions on one guest | Variable, dataframe update and NumPy array retained |
| Additional interpreter state | Lambda and open guest file retained, including its seek position |
| Capture boundary | State captured before changing the variable/dataframe to `999`; restored values match the checkpoint, not the later mutations |
| Abrupt process termination | Supervisor killed only its owned helper after the snapshot and atomic readiness report completed |
| Separate-process restore | New helper restored the on-disk snapshot and passed the same Python assertions |
| Saved checkpoint stability | All recorded checkpoint-file hashes remained unchanged after restoration |
| Truncated index | Exit 1, explicit JSON parsing error, no success report |
| Missing snapshot blobs | Exit 1, explicit missing-blob error, no success report |
| Altered compatibility key | Exit 1, explicit kernel/host-contract mismatch, no success report |

The initial recovery execution passed; a second complete execution also passed and included all three refusal controls. The [retained result](mxc_native_state/windows-result.json) omits transient process identifiers and records that the helpers were distinct processes. The helper executable SHA-256 was `77db7ece296c0ce7a1bea4834afb243773e78bdd9c280a3943e72541fd78f751`; the snapshot compatibility key was `k5e9192dfed5c8dbb-c1`. The second snapshot's main memory blob occupied 915,845,120 bytes. This is one observed footprint for this workload, not a capacity estimate or performance benchmark.

`cargo build --locked`, `cargo fmt --check`, `cargo clippy --locked -- -D warnings`, supervisor lint/format/type checks, real helper `--help`, and a safe invalid-argument control were used for validation. The snapshot and diagnostic files remain outside Git; the retained structured result contains artifact hashes without host locations.

### Integration consequence

Rich Python continuity and modified-state restoration are feasible on the tested Windows host through the underlying library. They are not supported persistent-session behavior of the unchanged MXC runner. The maintainer selected extending MXC's Hyperlight session API on 2026-10-02. The production adapter will use that MXC surface; the lower-level helper remains an experiment. The owning design in [PR #1650](https://github.com/sokolaidev/maf-extensions/pull/1650) records the proposed responsibility boundary and checkpoint barrier. No production session API has been implemented.

## Remaining evidence

Lost acknowledgments, atomic checkpoint commitment after every successful tool call, host file/artifact consistency, compatible second-machine recovery, fencing, resource limits, general owner-death cleanup and network enforcement remain unrun. Linux/KVM, host reboot and power-loss durability have not been tested. The open-file case covers an in-guest file, not a host mount or arbitrary sockets/threads. Refusal controls cover malformed metadata, missing blobs and a mismatched compatibility key; payload corruption and adversarial snapshot inputs need separate qualification. Guest stdout/stderr separation still needs a supported native output channel or upstream change before promising the existing result contract.
## Upstream feature-request draft

Proposed title: **Hyperlight: opt-in persistent sessions with checkpoint export and restore**

The following proposal is prepared for MXC's feature-request template. It has not been posted. The pinned [contributor guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/CONTRIBUTING.md#before-you-start-file-an-issue) asks for an issue before implementation and a written design for larger changes. Searches of open and closed MXC issues for Hyperlight snapshot/persistence did not identify an equivalent proposal on 2026-10-02. The runner at current main `298bb3909a5130cc6caed575494290fbcacf0db5` still restores its rewind baseline before subsequent executions.

### Description of the new feature / enhancement

Add an opt-in experimental Hyperlight session API that preserves interpreter state across executions and can export/restore a checkpoint of modified state. Keep existing one-shot requests fresh by default.

Agent code-execution hosts need notebook-style Python continuity and recovery after a host process restarts. MXC's current Hyperlight runner reuses a guest but restores its baseline between executions, so retaining a runner does not retain Python variables. Its prepared startup snapshot is a different operation from capturing modified session state.

We have a reproducible Windows/WHP feasibility probe using the agent rootfs from MXC v0.9.0 and pinned hyperlight-unikraft 0.14.1. A variable, pandas dataframe, NumPy array, lambda and open in-guest file survived multiple executions and disk checkpoint restoration in a new process after the original helper was killed. Changes after capture did not appear in recovered state. Malformed metadata, missing blobs and a changed compatibility key were refused. [Probe, dependency pins and evidence](https://github.com/sokolaidev/maf-extensions/blob/2797c74f8d0503d22e9b3a76c1457aa5723a05e1/scripts/experiments/mxc_hyperlight_probe.md#native-state-and-restart-experiment).

These results use the underlying library directly; they do not establish a supported MXC session API, Linux qualification, host reboot/power-loss durability, remote recovery or arbitrary thread/socket recovery. We intend to contribute an implementation after agreeing the API boundary and test plan.

### Proposed technical implementation details

Start with an explicit Hyperlight native session abstraction. Bind the runtime/artifact identity and admitted policy at creation; reject incompatible changes instead of replacing the session with empty state. Preserve the existing one-shot runner's behavior and timeout handling.

Expose the following semantics, with method names and SDK placement to agree:

- Create or restore a session with an explicit runtime and currently admitted policy.
- Execute without baseline rewind, reporting execution completion separately from durable application success.
- Park the session and export an immutable checkpoint candidate with its runtime/artifact and compatibility metadata.
- Keep execution blocked while the candidate is uncommitted; let the trusted host confirm publication for that candidate/generation before resuming. The confirmation is the host's assertion about its store, not MXC independently certifying storage durability.
- Close or explicitly recover after execution, timeout, cancellation or capture failure; never silently reset a persistent session or replay its program.

MXC would own native session lifetime, interruption, checkpoint capture and compatible restore. Applications would own their durable store, conversation identity, atomic checkpoint/result publication, idempotent result delivery and distributed fencing. No cloud storage dependency is needed in MXC. Host mounts and external side effects require separate consistency rules; a VM snapshot alone cannot roll them back.

The first increment can qualify these native primitives without mounts and with networking closed. Then expose them through MXC's experimental engine/SDK contract and supervised out-of-process transport. Any JSON additions should use permanent locations in the mutable development schema, keep runtime experimental authorization, and include matching TypeScript SDK/contract tests. Existing stable schemas and one-shot requests must remain compatible. Do not assume existing generic provision/exec lifecycle operations already provide Hyperlight session semantics.

Acceptance should include a fresh-state control, same-session variables/dataframes, restore after helper death, immutable capture boundaries, rejection of malformed/incomplete/incompatible checkpoints, failed capture/commit barriers, policy-change refusal, cancellation and disposal on Windows/WHP and Linux/KVM. An initial backend-level patch should exercise these paths before changing the wider SDK surface.

Output fidelity needs a related transport work item: with the released executor, guest stdout and stderr markers both arrive on process stdout. Control messages must remain independent of guest text, and stream separation needs its own demonstrated implementation. Conditional allowlisting remains separately qualified; this proposal does not enable networking.