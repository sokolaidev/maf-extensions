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

Next experiment: retain one `AppSandbox`, set a variable and dataframe, verify them on a second execution, write a snapshot after success, terminate the helper, then restore in a new process and verify the same values. Keep guest networking disabled and mounts absent for this first state test. Bind the snapshot to runtime identity and preserve the original committed snapshot during failure tests. A later experiment must coordinate session files and host-controlled atomic commitment; the library's disk snapshot alone does not establish that application contract.

A helper that calls Unikraft directly would bypass MXC's current runner. Treat it as an experimental feasibility probe and identify the MXC API change or alternative integration needed before proposing it as the production adapter. No upstream issue or PR has been published.

## Remaining evidence

Same-guest continuity, modified-state disk restore, lost acknowledgments, atomic commitment, file/artifact consistency, compatible second-machine recovery, fencing, resource limits, owner-death cleanup and network enforcement remain unrun. Linux/KVM has not been tested. Rust tooling was not found on the current shell PATH; building the proposed native helper needs a checked toolchain. Guest stdout/stderr separation needs a supported native output channel or upstream change before promising the existing result contract.