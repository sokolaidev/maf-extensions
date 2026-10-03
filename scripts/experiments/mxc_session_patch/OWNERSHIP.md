# MXC native ownership experiment

The `call-owned` helper mode requires the eight-byte `MXCOWN1\n` header on stdin before loading a snapshot. The host keeps the private pipe's writer open for the entire native call. EOF, a pipe error or the one-byte cancellation command `C` terminates the native process with exit code 74; an invalid header or command exits with 75. Normal completion retains the existing separate native-written capture report. The guest has no access to this host-side pipe and its output cannot supply ownership commands.

The watcher terminates the process without waiting for guest execution to unwind. Process exit closes the helper's VM handles. A killed owner therefore cannot leave this native process executing indefinitely merely because the Python program has not returned. The compatibility `call` mode remains available for historical experiments; the host publication supervisor uses `call-owned` exclusively. This adds no dependency on the temporary MXC session API outside the existing Rust wrapper.

Ownership loss cannot acknowledge a tool call: the helper cannot publish the database transaction, and its host publishes only after a successful helper exit and capture report. A candidate left by interruption during capture is uncommitted scratch data. This mechanism does not provide distributed fencing, supervision of arbitrary descendants, prompt cleanup under host scheduler starvation, output fidelity, native output-buffer bounds or generic file confinement. Multiple trusted writers holding the pipe open would extend ownership, so the host must not delegate or inherit that writer into other processes.

[owner_probe.py](owner_probe.py) uses a separate owner process holding the only writer. After the fixed guest emits its readiness marker and enters a loop, the probe kills the owner or sends a cancellation/invalid command, requires native exit within five seconds, and verifies no candidate or completion report exists. The guest marker synchronizes this fixed test only; it is not production control status. Missing ownership and an invalid header must refuse before attempting to load even an absent snapshot. The normal publication probe independently exercises successful owned calls and saved-result retries.

```text
cargo test --locked --manifest-path <generated-build>/Cargo.toml
python scripts/experiments/mxc_session_patch/owner_probe.py --helper <built-helper> --startup <prepared-agent-snapshot> --state-dir <new-evidence-directory>
```

The opt-in `mxc_recovery` workflow input runs these controls on the GitHub Linux/KVM runner after native recovery and host publication. Windows/WHP is independently qualified with the same fixed probe. Runtime reports are retained with the exact executable hash; only reports and diagnostics are uploaded, never checkpoints. Track completion in [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669).

The [Windows owner report](windows-owner-result.json) records all five controls passing. Owner death, cancellation and an invalid command stopped the live helper within 0.18 seconds in this measurement; five seconds is the probe deadline, not a real-time production guarantee.

The [Windows publication report](windows-owned-publication-result.json) also passes the full publication/crash/redelivery probe using `call-owned` and the same helper hash. This covers normal completion and lost acknowledgments alongside the independent live interruption controls.

The [Linux/KVM report](linux-owned-result.json) records all five ownership controls, native state/restart controls, and publication/crash/redelivery controls passing in [run 37085668203](https://github.com/sokolaidev/maf-extensions/actions/runs/37085668203) at `4bead84e554b4553d0c12c0beca69a36a4fb3f5c`. The three live stops were below 0.016 seconds in this measurement. Later commits update only documentation/evidence; these timings are observations, not portable latency guarantees. The unchanged source patch remains replaceable independently of the owner pipe.
