# MXC 1.0 migration experiment

This is the separately pinned candidate for [spike #1780](https://github.com/sokolaidev/maf-extensions/issues/1780). The [research record](../../../docs/sandbox/research/mxc-backend.md#mxc-10-migration-spike) owns the adoption recommendation. The [0.9 experiment](../mxc_session_patch/HOST_PUBLICATION.md) and its reports remain unchanged.

`manifest.json` pins MXC 1.0.1, Unikraft 0.17.0, Hyperlight host/common 0.17.0 and each affected file before/after the session, output and storage layers. `rootfs.json` pins the matching Linux/amd64 agent image and extracted initrd. `Cargo.lock` is the candidate dependency graph. There is no package release or production adapter in this experiment.

## Apply and reverse

Use fresh checkouts at the manifest's exact commits. From the repository root, invoke `python -m scripts.experiments.mxc_v1_patch.patch` with the action, `--layer session|output|storage`, and explicit `--source`, `--runtime` and `--host` checkout paths. Apply in that order and remove in reverse order. Every operation validates the entire known file state and patch checksums before changing any checkout; modified prerequisites and mixed layers are refused. Unrelated files are outside this integrity check.

After all layers are applied, use action `configure` with `--build-dir` naming a new directory. The build reuses the existing `probe/main.rs`, `probe/backend.rs` and `probe/owner.rs`, replacing only backend imports and the recorded MXC source identity. The single Rust wrapper uses the consolidated SDK's internal compatibility exports; the stable V1 API does not promise this surface. Build with `cargo +1.98.0 build --locked --manifest-path <build-dir>/Cargo.toml`.

## Hosted qualification

Dispatch `tests.yml` on the candidate branch with `mxc_v1=resolve-lock` to prepare a dependency lock candidate. This mode reports `dependencies-resolved`, never `qualified`, and performs no native qualification. Review and commit the resulting `build/Cargo.lock` before dispatching `mxc_v1=qualify`. Qualification must use the retained lock with `--locked`.

The native mode independently checks KVM or WHP, builds the new helper, runs collector/budget unit tests, prepares the verified rootfs, and runs the existing rich-state, bounded-output, bounded-export and format-4 durability controls. It then builds the old pinned helper and produces an old checkpoint. The new helper must refuse that incompatible checkpoint without changing it; the old helper must still restore it. All layers must be removable afterward. A failed stage retains an `unqualified` result and diagnostics.

Artifacts contain reports, logs and the resolved lockfile for 14 days, excluding guest snapshots and SQLite stores. The historical MXC 1.0.0 reports below come from [run 37752153210](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210), source `da064543023d2b95ae06735d2746f520caa4eb61`; later documentation/report commits do not change the tested harness.

| Platform | Migration controls | Rich Python state | Outcome |
|---|---|---|---|
| Linux/KVM | [Report](linux-result.json) | [Report](linux-native-result.json) | Qualified |
| Windows/WHP | [Report](windows-result.json) | [Report](windows-native-result.json) | Qualified |

Both platforms refused the old `k5e9192dfed5c8dbb-c1` snapshot under the new `k485adb2bab362516-c3` runtime without changing the checkpoint; the old-pin helper then restored its state. Existing persistent sessions require their exact old helper/profile assets. The experiment does not convert guest snapshots or silently substitute fresh state. Process crashes are not physical reboot or power-loss tests, and independently passing on two platforms does not establish checkpoint portability between them.

## MXC 1.0.1 refresh

The active SDK pin is release `v1.0.1`, commit `fda29ac63bc306b7f97f3b86b4b63d44f7f40cf2`. The runtime, Hyperlight host/common, rootfs and downstream byte-stream/file kernel pins stay unchanged. The [release analysis](../../../docs/sandbox/research/mxc-backend.md#mxc-101-release-analysis) describes the source changes and remaining limits. All temporary layers remain necessary; their apply/remove checks cover the updated SDK. The historical reports above qualify their recorded 1.0.0 sources only. [Run 40082577780](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780), source `7545863209d86738bf4f37ae2ed788675e552734`, qualified the updated SDK with the retained lock on both platforms. Later report/documentation commits do not change the tested harness.

| Platform | Session and migration | Rich state | Byte streams | Files and format-7 recovery | Volume cleanup |
|---|---|---|---|---|---|
| Linux/KVM | [Qualified](v1.0.1-linux-result.json) | [Passed](v1.0.1-linux-native-result.json) | [Qualified](../mxc_streams_patch/v1.0.1-linux-result.json) | [Qualified](../mxc_files_patch/v1.0.1-linux-prelaunch-result.json) | [Detached and removed](../mxc_files_patch/v1.0.1-linux-prelaunch-cleanup.json) |
| Windows/WHP | [Qualified](v1.0.1-windows-result.json) | [Passed](v1.0.1-windows-native-result.json) | [Qualified](../mxc_streams_patch/v1.0.1-windows-result.json) | [Qualified](../mxc_files_patch/v1.0.1-windows-prelaunch-result.json) | [Detached and removed](../mxc_files_patch/v1.0.1-windows-prelaunch-cleanup.json) |

Each platform passed 23 byte-stream scenarios, 24 file/control cases, durable replay/crash controls, format-7 pre-launch checkpoint-copy recovery, physical-storage checks, deletion and idle lifecycle controls. Migration also passed mutual-TLS native publication/restore/replay and verified local cleanup; the incompatible 0.9 checkpoint stayed unchanged and recovered with its old helper. These are independent same-machine controls on each platform, not a fresh cross-runner qualification or an in-place 1.0.0-to-1.0.1 session migration.
