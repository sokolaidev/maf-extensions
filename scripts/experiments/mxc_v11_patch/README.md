# MXC 1.1 upgrade candidate

This separately pinned candidate implements [#2761](https://github.com/sokolaidev/maf-extensions/issues/2761). The existing [1.0.1 experiment](../mxc_v1_patch/README.md) and its runtime assets remain available for old-session recovery. This experiment does not release a package or migrate existing checkpoints.

`manifest.json` pins MXC 1.1.0, Hyperlight-Unikraft 0.21.0 and Hyperlight host/common 0.19.0. `rootfs.json` pins the Linux/amd64 agent image's index, manifest, layer and extracted initrd. Each patch records affected-file hashes before and after application. The [release analysis](../../../docs/sandbox/research/mxc-backend.md#mxc-110-upgrade-candidate) records the adoption boundary.

## Reversible layers

Use fresh checkouts at the manifest's exact commits. Run `python -m scripts.experiments.mxc_v11_patch.patch apply --layer session|output|storage --source <mxc> --runtime <unikraft> --host <hyperlight>`, in that order. Use `remove` in reverse order. `configure` takes the same paths and a fresh `--build-dir` and produces the helper build with the retained dependency lock.

- Session continuity remains an opt-in SDK patch; MXC's ordinary Hyperlight runner still rewinds between calls.
- Output uses upstream `output_limit`, `echo_output(false)` and `take_output`. The old capture implementation is removed from this candidate. A small runtime patch preserves saturation reporting for omitted-byte counts; the adapter maps upstream field names to the existing helper protocol. Upstream separately captures stderr, so the configured limit applies to each capture, not their aggregate allocation.
- Bounded storage charges all snapshot memory blobs, the new transport blob, configuration, manifest, layout and index before writing. A refused or partial checkpoint never becomes a successful publication.

The host journal, freshness protection, replay, ownership and mutual-TLS transfer controls are reused. Hyperlight networking remains closed. The byte-stream/file custom kernels and their existing qualification stay on 1.0.1; this candidate does not claim those kernels are compatible with 0.21.

## GitHub runner qualification

Dispatch `mxc-native.yml` with `mxc_v11=resolve-lock` first. This only resolves dependencies and reports `dependencies-resolved`. Review and retain the produced `build/Cargo.lock`, then dispatch `mxc_v11=qualify`; native builds use `--locked`.

[Resolution run 43769791355](https://github.com/sokolaidev/maf-extensions/actions/runs/43769791355) independently produced the same lock on Linux and Windows: SHA-256 `ff7ed2c0e6a093f99ea7cce17f7cf1b01ba89ec91ac3d1584a2b603f23d2ceb2`. The [recorded OSV check](dependency-audit.json) found no known advisories for its 289 registry crates at the recorded time. Refreshing the inherited graph replaced affected `libz-sys`, `rustix` and `tokio` versions with 1.1.33, 1.1.6 and 1.54.1 respectively. This check excludes path patches, system libraries, kernel and rootfs; it is not a whole-image security assessment.

Qualification runs independently on Linux/KVM and Windows/WHP: helper build, overflow/budget tests, rich Python state, bounded output and exports, durable replay/crash controls, and mutual-TLS checkpoint publication/restore/replay. It builds the retained 1.0.1 helper to produce an old checkpoint, requires the new helper to refuse it unchanged, and requires the old helper to restore it. Finally, every patch is removed and checked. Reports retain source, helper, dependency and image identities; failure remains `unqualified`.

Cross-runner checkpoint portability, physical reboot/power loss, byte-stream/file kernel migration and production deployment are separate acceptance boundaries. Existing sessions require their exact original helper/profile assets. A rejected old checkpoint must never be replaced silently with fresh state.
