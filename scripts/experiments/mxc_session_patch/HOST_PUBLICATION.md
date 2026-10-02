# MXC local checkpoint publication experiment

This continuation of [spike #1649](https://github.com/sokolaidev/maf-extensions/issues/1649) exercises the host side of the [removable native patch](README.md). It remains repository tooling, not a shipped adapter or a replacement for the selected first-version requirements in [the owning design](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/mxc.md).

## Publication and recovery

[host_store.py](host_store.py) stores compressed, hashed snapshot chunks and a bounded result in one local SQLite database. SQLite uses rollback journaling and `synchronous=FULL`; committing the transaction publishes both the current checkpoint reference and the saved result. Snapshot bytes are inside that transaction, so there is no separately renamed directory whose durability the database assumes. Result JSON includes the fixed experiment's chart artifact bytes.

A local operating-system file lock covers the entire owner lifetime. Acquisition increments a generation; stale-generation operations refuse. This is single-machine ownership on a private local filesystem, not a distributed lease or permission to recover on another machine. The profile binds the host-supplied session identity, helper hash, startup-index hash, platform, machine and closed/no-mount policy. An incompatible profile refuses; startup-index identity is not independent verification of every runtime blob or production policy admission.

Before native execution, the store records a pending call with its source hash. The native helper restores the latest verified committed state, executes once, exports a checkpoint and closes. It cannot execute another program from the uncommitted candidate. The host publishes checkpoint plus result, then acknowledges. This experiment deliberately restores a guest for each call; it does not implement warm resident-session confirmation or performance optimizations.

| Interruption boundary | Recovery behavior |
|---|---|
| Before the checkpoint/result transaction commits | Previous committed state remains current; interrupted call identity cannot replay automatically |
| After commit, before acknowledgment | Same call identity and source receive the saved result without starting the helper |
| A previously committed call retried after later calls | Original saved result is returned; the latest checkpoint remains current |
| Same call identity with different source | Refused |
| Missing metadata, changed profile, corrupt result or snapshot | Refused; no fallback to fresh state |

The operating system releases the lock when the host process dies. An old native helper has no database access, but killing native descendants when their owner dies during execution is not yet qualified. The live fault stops occur after the helper has closed. Network access and host mounts are absent, so this test does not address externally visible guest side effects or distributed fencing.

## Native files and output findings

The fixed guest program writes a supplied CSV to `/tmp/input.csv`, reads it with pandas, writes `/tmp/chart.png` with matplotlib, and exports the PNG as guest-authored data. The host checks chunk order, byte limits, PNG signature and the final hash before including the chart in the transaction. Retried calls return the same saved artifact bytes and result hash. This exercises guest file persistence and artifact publication; it is not qualification of the general FILES_IN/FILES_OUT API, arbitrary paths, host mounts or concurrent writers.

A first chart transfer exposed a reproducible output limit: a single long base64 write arrived as only 4,096 bytes, without a complete artifact. The experiment now sends chunks smaller than that boundary and refuses incomplete transfers. Guest stdout and stderr still share the native output path. [host_call.py](host_call.py) therefore names the captured field `combined_output`; it does not promise the suite's separate-stream result contract. Guest chart records are untrusted data and never supply execution or checkpoint status. Those statuses come from process exit and a separate native-written control report.

The Python supervisor bounds each process pipe and enforces a wall deadline. This does not establish native buffer limits, byte-exact arbitrary guest output, process-tree cleanup or a supported production control transport. These remain blockers for advertising the adapter's execution contract.

## Reproduce

Apply the pinned source patch and generate a new build directory as described in [the patch guide](README.md#apply-and-reproduce). The helper now also supports `call <restore> <checkpoint> <code-file> <report>`. Build from this continuation's manifest template so it includes that mode.

```text
cargo build --locked --manifest-path <generated-build>/Cargo.toml
python scripts/experiments/mxc_session_patch/host_probe.py --helper <built-helper> --startup <prepared-agent-snapshot> --state-dir <new-evidence-directory>
uv run pytest -q tests/test_mxc_host_store.py
```

The live probe creates a new private store, kills only its own host-call processes at named publication boundaries, and restarts with identical call identities. The fixed programs assert Python counter values so replay would fail. It verifies that result redelivery creates no native checkpoint candidate and that the saved chart remains byte-identical. Keep database, snapshots, diagnostics and generated programs outside Git: they contain session state and may contain host paths.

## Hosted Linux runner

The opt-in `mxc_recovery` input on the Tests workflow builds the locked helper against the pinned MXC commit, requires usable KVM, downloads and verifies the fixed agent rootfs, then runs both native recovery and host publication probes. Missing KVM or any failed probe fails the job. The run retains reports, hashes and diagnostics for 14 days; checkpoint blobs and the session database are excluded.

```bash
gh workflow run tests.yml --repo sokolaidev/maf-extensions --ref spike/1649-mxc-durable-publication -f mxc_recovery=true
```

## Windows evidence

The [retained Windows result](windows-host-result.json) records the helper, source and store hashes. All real host-process crash cases passed: before commit, after commit and before acknowledgment. The latest committed Python counter survived each boundary, committed retries did not execute the helper, and the original PNG result remained identical when retried after later calls. The initial 4,096-byte output truncation was refused rather than committed as an artifact.

Fifteen focused store/protocol tests passed, including abrupt process exits inside a SQLite transaction, stale ownership, changed profiles, result/chunk corruption, missing session metadata, interrupted-call refusal, checkpoint limits, hard links, and dropped/duplicate chart chunks. Native locked build, formatting and strict Clippy passed. These are process-crash and fixed-workload results, not power-loss or production backend acceptance.

## Linux attempt

The [Linux attempt record](linux-attempt.json) records a compiled Linux target and verified KVM device access through a temporary process group, but no successful guest execution. The executable failed format inspection, and the next attempt encountered filesystem I/O errors. Local system storage also exhausted during validation. No Linux runtime pass or Windows/Linux checkpoint compatibility is inferred from compiler completion; rerun on a healthy Linux/KVM host before accepting that platform.

## Limits and next gate

SQLite FULL synchronization expresses a storage requirement; process-crash tests do not prove actual power-loss or device-cache behavior. Use only a private local filesystem for this experiment. Network filesystems, remote recovery, distributed fencing, encryption, retention/garbage collection, arbitrary snapshot input and production filesystem race resistance remain outside its contract. Checkpoint/result size limits exist; total historical storage is not yet bounded.

A production `maf-sandbox-mxc` adapter remains gated on bounded faithful output, independently framed native control, owner-death cleanup, safe file input/collection, and runtime evidence on both Windows/WHP and Linux/KVM. Closed networking remains the baseline. Conditional allowlisting needs its own enforcement evidence. The host store and behavior tests do not import the temporary MXC API; its eventual upstream replacement stays confined to the existing Rust wrapper and manifest selection.
