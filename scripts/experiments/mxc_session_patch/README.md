# Removable MXC session preview

Experimental work for [maf-extensions #1649](https://github.com/sokolaidev/maf-extensions/issues/1649) and the proposed upstream API in [microsoft/mxc #1374](https://github.com/microsoft/mxc/issues/1374). This bundle enables a native persistence probe; it is not a shipped backend, supported MXC API or durable conversation store.

## Removal boundary

[session-preview.patch](session-preview.patch) changes exactly three files at MXC v0.9.0 commit `86fb3d2abaf9c431556692037bff881830b543a5`: it adds the disabled-by-default `maf-session-preview` Cargo feature, a gated module declaration, and one new module. It does not change the existing one-shot runner, public JSON schemas, SDKs or dependencies. [patch.json](patch.json) binds the base revision and patch checksum.

Only [probe/backend.rs](probe/backend.rs) imports the temporary session API. Its session, checkpoint and error types belong to the wrapper, so the test program does not depend on upstream type names. The feature and dependency selection live in [Cargo.toml.template](Cargo.toml.template). No shipped maf-sandbox package imports this experiment.

## Session behavior

Restore takes an explicitly selected, trusted snapshot with no mounts or networking configured. Execution preserves Python state. Each successful execution blocks subsequent execution until capture produces a candidate and the host confirms publication of that same candidate on that same live session. Stale and foreign tokens cannot release the barrier. Guest errors, timeouts and capture failures retire the guest; recovery requires an explicit restore. Closing a session leaves caller-owned checkpoint files intact.

Confirmation is the host's assertion about its store. The probe confirms immediately after snapshot export and does not implement durable publication. A production host must atomically publish the checkpoint and result, then confirm, then acknowledge the successful tool call. It must own result redelivery, fencing, identity and compatibility checks. The snapshot directory is caller-owned and is not made immutable by this patch; the host must protect published checkpoints. Failed exports may leave incomplete directories for host cleanup.

## Apply and reproduce

Use a dedicated, clean MXC checkout at the exact base above. The tool checks revision, checksum and forward/reverse applicability; it refuses drift rather than attempting a three-way merge. The build directory must be new and outside this repository. It receives generated local paths and a copy of the retained [Cargo.lock](Cargo.lock), which pins `hyperlight-unikraft` 0.14.1 and Hyperlight host/common 0.17.0.

```text
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py apply --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py configure --source <mxc-checkout> --build-dir <new-build-directory>
cargo build --locked --manifest-path <new-build-directory>/Cargo.toml
```

Prepare the verified MXC agent image and startup snapshot using [the original experiment](../mxc_hyperlight_probe.md#reproduction). Supply that image home explicitly so the original runner's fresh-state control uses the same prepared runtime. The patched session restores the `snapshot` directory adjacent to the supplied initrd. The supervisor checks the recorded initrd hash; this is not independent verification of the startup snapshot's contents.

```text
python scripts/experiments/mxc_native_state_probe.py --helper <built-mxc-session-state-probe> --initrd <agent-initrd> --image-home <prepared-image-home> --state-dir <new-evidence-directory>
cargo test --locked --manifest-path <mxc-checkout>/src/Cargo.toml -p hyperlight_common --features maf-session-preview -- --test-threads=1
```

The Windows measurement used Rust 1.98.0 with `x86_64-pc-windows-gnu` and the portable GCC toolchain identified in [the native experiment](../mxc_hyperlight_probe.md#native-state-and-restart-experiment). Select that toolchain explicitly to reproduce it. The later [host publication continuation](HOST_PUBLICATION.md#linux-evidence) qualifies the fixed native and publication probes on Linux/KVM; MSVC remains unqualified. Checkpoint and diagnostic files stay outside Git; diagnostic output may contain host paths. The fixed-program supervisor is not production process-tree supervision or a bounded output transport.

## Measured evidence

[windows-result.json](windows-result.json) retains the Windows/WHP execution result and artifact hashes without host paths or transient process identifiers. The source patch applies and reverses on a pristine checkout at the pinned base, leaving its Git status clean; a different base is refused. The patched common crate's unit suite passed **22 tests**, including the two added state/token tests. Locked builds, Rust formatting and probe and patched-crate Clippy with warnings denied passed, as did Python lint/format/type checks, the generated-manifest check, real helper help and invalid-argument controls. Two complete Windows probe executions passed; the retained result is from the final executable.

| Live control | Result |
|---|---|
| Original MXC one-shot runner | Second execution cannot see the first execution's variable |
| Persistent Python | Integer, pandas update, NumPy array, lambda and open guest file/seek position survive |
| Commit barrier | Execution refused both before capture and before confirmation |
| Candidate identity | Earlier checkpoint token cannot confirm the next candidate |
| Helper death and restore | Supervisor kills its owned helper; a different process restores the saved state |
| Capture boundary | Post-capture mutations do not appear on restore; checkpoint file hashes remain unchanged |
| Existing capture destination | Capture fails, preserves the saved checkpoint and retires the session |
| Guest exception and timeout | Both retire the session and refuse further execution |
| Explicit close | Further execution refused |
| Truncated metadata, missing blobs, wrong compatibility key | Each refused without a success report |

These Windows controls establish native behavior on one tested host. They do not qualify power-loss durability, actual host reboot, another machine, Linux, mounted files, network enforcement, arbitrary threads/sockets, adversarial snapshot inputs, cancellation, resource quotas or stdout/stderr fidelity. Capturing after successful executions is exercised; atomic host-store commitment and acknowledgment recovery were outside this initial native measurement and are exercised by the continuation below. The pre-PR repository gate passed: 12,958 tests passed and 749 skipped, with lint, format, type and documentation checks also passing. Markdown-block and authenticated tracker checks passed separately. Hosted CI is a separate result.

## Host publication continuation

The [local publication experiment](HOST_PUBLICATION.md) adds checkpoint/result transactions, interrupted-call refusal, saved-result redelivery, and a fixed CSV-to-chart workflow. Its independent Windows/WHP and hosted Linux/KVM evidence extends these measurements. General transport, file and recovery limits remain documented there.

## Output transport continuation

The [descriptor experiment](OUTPUT.md) compares separate guest pipes and files with the existing console. It retains a minimal truncation reproducer and distinguishes cooperative byte fidelity from host-enforced output bounds. The [bounded capture overlay](OUTPUT.md#bounded-native-capture-overlay) adds a separately removable Rust-only collector with the accepted truncate-and-continue policy. Remove that overlay first if it is installed.

## Remove or replace

Stop sessions using the patched binary before switching providers. Removing source edits does not revoke an already-built binary or migrate saved state.

```text
python scripts/experiments/mxc_session_patch/patch.py remove --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
```

Removal reverses only this patch and refuses edited or mismatched hunks; it never forces a reset or deletes checkpoint files. Keep the pinned runtime available for outstanding sessions until their recovery or migration is resolved.

When upstream provides an equivalent supported API:

1. Replace the implementation of `probe/backend.rs` and the manifest's dependency/feature selection with the released API. Preserve the wrapper's commit barrier even if upstream locates publication acknowledgment elsewhere.
2. Run the same native controls against the new provider on Windows/WHP and Linux/KVM. Qualify checkpoint compatibility explicitly; migrate through a tested path or keep old sessions on the pinned runtime until they finish. Never replace an incompatible session with fresh state silently.
3. Reverse the source patch, rebuild without `maf-session-preview`, and remove `session-preview.patch`, `patch.json` and the patch application tool. Retain the behavior probes and historical evidence. A production adapter should depend on the replacement wrapper, never on `session_preview` directly.

The upstream issue contains our contribution offer. This downstream patch has not been submitted as an upstream PR.
