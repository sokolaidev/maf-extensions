# MXC separate byte-stream experiment

This profile extends the [MXC 1.0 experiment](../mxc_v1_patch/README.md) with distinct stdout/stderr byte channels. It is experimental qualification tooling, not a shipped sandbox backend. The [design](../../../docs/sandbox/backends/mxc.md#separate-byte-stream-profile) records the accepted policy and the [research](../../../docs/sandbox/research/mxc-native-output.md) records candidate-specific evidence.

Each stream retains at most 1,048,576 bytes per call. Further bytes are acknowledged to the guest, discarded by the host collector, and counted with an explicit saturation flag. Prefixes may end inside a UTF-8 sequence; consumers choose decoding policy after receiving bytes. An exact-limit write is not truncated. Malformed stream IDs or chunks larger than 4096 bytes latch a transport fault and prevent successful checkpoint publication. No ordering across the two streams is promised.

The native helper writes two bounded byte files and a separate control record of at most 1024 bytes. The parent rejects duplicate keys, unexpected fields, invalid types, counters, limits and payload lengths. Guest output is never parsed as native completion. Failed execution may retain diagnostic byte files but produces no successful control record. The format-4 supervisor reserves three MiB for the encoded result and accounts for payload, diagnostic, readiness and control files separately from checkpoint capacity. The existing two-MiB result default remains unchanged.

## Run on GitHub

Dispatch `tests.yml` at the candidate branch with `mxc_streams=true`. The Linux kernel job builds from `kernel.json` source pins, applies `kernel.patch`, records the observed builder/toolchain identity, and reverses the patch. The two native jobs download that same run's kernel artifact, verify its hash and source metadata, then build the helper with the retained MXC 1.0 dependency lock. Linux requires KVM; Windows requires WHP. Setting only `mxc_stream_kernel=true` builds the kernel without qualifying execution.

A native job reports `qualified` only after byte probes, the Python state/restart controls, the format-4 crash matrix and overlay removal all pass. The crash matrix carries two full retained byte streams, checks omission counters and result replay, and reconciles SQLite charges. Failure leaves an `unqualified` report and logs. Compilation or a successful job whose opt-in steps were skipped does not qualify this profile.

## Remove the overlays

`kernel_patch` manages an explicit runtime checkout with initialized, pinned submodules. `patch` manages explicit MXC, runtime and Hyperlight-host checkouts with the three MXC 1.0 experiment layers already applied. `configure` first creates a fresh helper build from that baseline, then applies the stream layer and installs the backend wrapper. Both tools check source commits, affected files and patch hashes before mutation.

```bash
python -m scripts.experiments.mxc_streams_patch.kernel_patch remove --source RUNTIME_SOURCE
python -m scripts.experiments.mxc_streams_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
```

Reverse the underlying MXC 1.0 layers afterward, in storage/output/session order. The qualification runner also restores the original embedded kernel file before reversing the Rust layers. A manually configured build must restore that saved kernel too. Source removal does not change existing executables or migrate checkpoints; keep the original runtime available for its saved sessions. Replace this profile with upstream support only after rerunning the same behavioral qualification.
