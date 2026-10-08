# MXC 1.0 migration experiment

This is the separately pinned candidate for [spike #1780](https://github.com/sokolaidev/maf-extensions/issues/1780). The [research record](../../../docs/sandbox/research/mxc-backend.md#mxc-10-migration-spike) owns the adoption decision. The [0.9 experiment](../mxc_session_patch/HOST_PUBLICATION.md) and its reports remain unchanged.

`manifest.json` pins MXC 1.0.0, Unikraft 0.17.0, Hyperlight host/common 0.17.0 and each affected file before/after the session, output and storage layers. `rootfs.json` pins the matching Linux/amd64 agent image and extracted initrd. `Cargo.lock` is the candidate dependency graph. There is no package release or production adapter in this experiment.

## Apply and reverse

Use fresh checkouts at the manifest's exact commits. From the repository root, invoke `python -m scripts.experiments.mxc_v1_patch.patch` with the action, `--layer session|output|storage`, and explicit `--source`, `--runtime` and `--host` checkout paths. Apply in that order and remove in reverse order. Every operation validates the entire known file state and patch checksums before changing any checkout; modified prerequisites and mixed layers are refused. Unrelated files are outside this integrity check.

After all layers are applied, use action `configure` with `--build-dir` naming a new directory. The build reuses the existing `probe/main.rs`, `probe/backend.rs` and `probe/owner.rs`, replacing only backend imports and the recorded MXC source identity. The single Rust wrapper uses the consolidated SDK's internal compatibility exports; the stable V1 API does not promise this surface. Build with `cargo +1.98.0 build --locked --manifest-path <build-dir>/Cargo.toml`.

## Hosted qualification

Dispatch `tests.yml` on the candidate branch with `mxc_v1=resolve-lock` to prepare a dependency lock candidate. This mode reports `dependencies-resolved`, never `qualified`, and performs no native qualification. Review and commit the resulting `build/Cargo.lock` before dispatching `mxc_v1=qualify`. Qualification must use the retained lock with `--locked`.

The native mode independently checks KVM or WHP, builds the new helper, runs collector/budget unit tests, prepares the verified rootfs, and runs the existing rich-state, bounded-output, bounded-export and format-4 durability controls. It then builds the old pinned helper and produces an old checkpoint. The new helper must refuse that incompatible checkpoint without changing it; the old helper must still restore it. All layers must be removable afterward. A failed stage retains an `unqualified` result and diagnostics.

Artifacts contain reports, logs and the resolved lockfile for 14 days, excluding guest snapshots and SQLite stores. Successful reports will be retained here with exact source/run identities. Native evidence is pending. Process crashes are not physical reboot or power-loss tests, and independently passing on two platforms does not establish checkpoint portability between them.
