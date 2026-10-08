# MXC bounded file experiment

This profile adds bounded input files, a virtual workspace and named artifact delivery to the [separate-stream experiment](../mxc_streams_patch/README.md). It is repository qualification tooling, not a production backend. The [design](../../../docs/sandbox/backends/mxc.md#runtime-and-files) records the selected policy; the [research](../../../docs/sandbox/research/mxc-backend.md#native-file-candidate) records implementation boundaries and candidate-specific evidence.

## File contract

`Request` accepts immutable input bytes, portable relative names, host-selected call/session lifetimes and explicit replacement authority. Each direction defaults to 64 files and 16 MiB, including a 16 MiB per-file ceiling. The live workspace defaults to 256 files and 64 MiB. All selected bounds enter retry identity. The host may configure limits within the experiment's fixed ceilings.

The helper exposes `guest_call_path` and `guest_session_path` and starts code in the call directory. Input files are writable. Session inputs use the session directory; other inputs and requested artifacts use the call directory. An upload replacing a retained logical filename needs exact spelling and explicit authority, including when the new upload is call-scoped. Files keep their lifetime when renamed. A guest-created copy is independent state.

Guest paths name entries in a virtual service, never host paths. The service refuses links, special files and growth beyond its logical limits. Whole writes are staged before mutation; capacity refusal is a catchable filesystem error. Earlier successful operations, including an explicit truncate, are not rolled back by a later write error. Directory metadata is separately bounded. The service permits one staged write at a time; other mutations return `EBUSY` until it commits or aborts. Permission bits are synthesized; `chmod` does not persist permission changes. The upstream path-based ABI does not promise full POSIX inode behavior for retained descriptors after unlink/recreation or every directory rename.

The helper validates all artifacts while the guest is stopped. It copies their bytes, reclaims call files, and captures retained workspace files alongside VM state. The format-4 store publishes that checkpoint and the result envelope atomically before acknowledgment. Matching retries return identical artifact bytes without a helper; failure preserves the previous committed state. Workspace limits do not bound all guest-private filesystems or total process memory.

## Session deletion

The experimental shared-call supervisors accept a host-owned `threading.Event` through `delete=`. The owner thread observes it before admission, during native execution, before publication and before acknowledgment. Observation durably retires the session before interrupting execution. A request committed after the call result preserves that result for retry; deletion never changes the selected retry window. The event is in-process notification, not a durable cross-process request queue: a request lost before retirement must be resubmitted by the host.

`NativeJournal.delete()` resumes deletion while holding the session ownership lock, including after restart. It stops a recorded helper through a creation-checked Windows process handle or Linux pidfd, waits for confirmed termination, and then reclaims its private scratch through the existing cleanup journal. A missing/corrupt identity, unavailable process evidence, unknown scratch entry or failed termination keeps unresolved capacity charged. This method does not terminate arbitrary descendants or fence another machine. Checkpoint collection remains a separate bounded operation; completed results and call identities survive it. These are experimental host controls, not a production backend API.

## Run on GitHub

Dispatch `tests.yml` at the candidate branch with `mxc_files=true`. The Linux job builds the pinned combined kernel and retains its source, builder and toolchain identity. Linux/KVM and Windows/WHP jobs consume the same kernel artifact, build the helper with the retained dependency lock, and run native file and recovery probes. No local Rust build is required. The [qualified candidate record](../../../docs/sandbox/research/mxc-backend.md#qualified-file-candidate) links the retained reports and [example chart](example-chart.png).

A native job reports `qualified` only after file probes, Python-state controls, the eight-boundary crash matrix, failed-artifact recovery, active deletion/restart controls and overlay removal pass. The artifact includes bounded failure diagnostics and the collected CSV chart. Compilation or a successful job with opt-in steps skipped is not native qualification.

## Remove the profile

The file `patch` tool requires the three MXC 1.0 layers and the stream layer. Its `configure` action creates a fresh helper from the MXC 1.0 baseline and installs both stream and file layers. Every mutation checks pinned commits, patch hashes and affected source fingerprints.

Restore the runtime checkout's saved original embedded kernel before removing its Rust layers. For the separate kernel-source checkout and native-source checkouts:

```bash
python -m scripts.experiments.mxc_files_patch.kernel_patch remove --source KERNEL_RUNTIME_SOURCE
python -m scripts.experiments.mxc_files_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
python -m scripts.experiments.mxc_streams_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
```

Remove the underlying MXC 1.0 layers in storage/output/session order. The qualification runner performs this sequence and checks removal. Removing source patches does not change existing executables or migrate saved sessions. Replacing the profile with upstream support requires equivalent behavioral qualification and an explicit checkpoint-compatibility decision.
