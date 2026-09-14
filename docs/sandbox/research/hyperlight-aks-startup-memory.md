# Hyperlight Python startup and memory on AKS

> Measured on 2026-09-14 for [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230), following the [KVM execution proof](hyperlight-aks-kvm-verification.md). The [benchmark](hyperlight-aks/benchmark.py) and [results with individual samples](hyperlight-aks/benchmark-results.json) preserve the experiment. These are packaged SDK measurements, not Linux adapter or CodeAct conformance.

## Result

A fresh Python sandbox in an already running container took 916 ms at the median through its first successful statement. Restoring a baseline and executing new code took 3.63 ms at the median after the first restore; that first restore plus execution took 134 ms. Retaining guest state and running another statement took 0.188 ms at the median. An initialized guest used 857 MiB of host-process RSS, retaining its first snapshot raised that to 1,328 MiB, and the application container reached a 1,783 MiB lifetime peak across the experiments. The measured profile therefore trades substantial retained memory for fast reset between calls.

The pinned SDK initializes lazily: its [native `run` implementation](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/sdk/python/wasm_backend/src/lib.rs#L99) constructs the native sandbox on first execution. Timing `Sandbox(...)` alone measures an inexpensive configuration object and misses almost all startup work. Generic Hyperlight micro-VM startup figures cannot substitute for this Python guest measurement.

## Configuration

The cluster was AKS Automatic with Azure Linux 3.0, kernel `6.6.150.1-1.azl3`, Kubernetes/kubelet 1.35.7 and containerd 2.2.4. One automatically provisioned `Standard_D4als_v6` workload node supplied 4 vCPUs and 8 GiB of nominal memory. Five application pods ran sequentially on that node; the original seed pod kept it available between samples. This is one node and one VM size, not a cross-node performance distribution.

The immutable Python image, plugin image and hash-pinned 0.7.0 SDK trio matched the earlier execution proof. Host Python was 3.13.12. Each application container requested and was limited to one CPU and 2 GiB, with `heap_size="400Mi"` and `stack_size="200Mi"` passed to the SDK. Those two guest configuration values are not a bound on total host-process or container memory. The AOT guest artifact was 43,890,120 bytes, separately from runtime memory.

The application ran as UID/GID 65534 with a read-only root filesystem, all capabilities dropped, no privilege escalation, RuntimeDefault seccomp and no service-account token. The main container requested one `hyperlight.dev/hypervisor` allocation. Packages were installed by the unprivileged init container; a deny-all network policy was applied after pod readiness and before benchmark execution. The only infrastructure admission exception was the same temporary test namespace used in the earlier proof.

## Startup and execution latency

| Measurement | Samples | Median | p95, nearest rank | Observed range |
| --- | --- | --- | --- | --- |
| Pod creation to readiness, existing node | 5 | 7 s | Not estimated for deployment planning | 7–11 s |
| First seed pod, including node provisioning | 1 | 91 s | Not estimated | One observation |
| Fresh process: SDK/native imports, guest materialization, creation and first statement | 5 | 1,033 ms | Not estimated | 1,022–1,055 ms |
| Fresh sandbox and first statement, imported SDK in a warm process | 30 | 916.37 ms | 943.03 ms | 900.40–948.94 ms |
| Capture initialized baseline snapshot | 1 | 568.72 ms | Not estimated | One observation |
| First baseline restore and new statement | 1 | 134.40 ms | Not estimated | One observation |
| Subsequent baseline restore and new statement | 29 | 3.63 ms | 5.48 ms | 3.23–5.63 ms |
| Another statement with retained guest state | 30 | 0.188 ms | 0.268 ms | 0.151–0.372 ms |

Pod timings come from saved Kubernetes timestamps with one-second resolution. They include a 4–5 second init-container package installation. Readiness meant the holding application container had started and the proof source was available; it did not mean the Python guest was initialized. The init-container completion to main-container start interval was 0–2 seconds. These intervals do not isolate raw container-runtime startup or represent an image with dependencies already installed.

The fresh-process total is the sum of five explicitly timed SDK stages. Each sample started with an absent private guest cache. It excludes interpreter startup, benchmark imports and validation, subprocess transport, and package installation. The node's filesystem and image caches were not flushed. Existing-cache materialization is also recorded separately before the warm-process experiment; it is excluded from that experiment's per-sandbox timings.

SDK measurements use a monotonic high-resolution clock around each operation. Memory inspection, result validation, output, garbage collection and disposal are outside those intervals. Fresh sandboxes execute `value = 42; print(value)`. Restore samples check that `value` is absent before assigning and printing the new value. Retained-state samples only print the existing value. The comparison therefore includes a small additional correctness check in the restore program. No remote model, HTTP serving, host tool, filesystem workload or concurrent request is included.

The first restore is shown separately rather than hidden inside the warm distribution. It spent 127.96 ms restoring and 6.44 ms executing; subsequent restores were much cheaper. During the entire 30-round latency phase, the application cgroup recorded five throttled CPU periods and 19.85 ms of throttled time, against 30.60 seconds of CPU use. The one-CPU limit is part of the measurement, not a claim about unrestricted performance.

## Memory footprint

The following values are medians from five independent benchmark processes in five sequential pods. MiB means 1,048,576 bytes. Process RSS measures resident mappings, while the application-container cgroup includes its charged memory and descendants. These counters describe different scopes and should not be added together. [Linux documents RSS/PSS](https://docs.kernel.org/filesystems/proc.html) and [cgroup memory accounting](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory) separately.

| Point | Process RSS | Application-container current memory |
| --- | --- | --- |
| Host Python before SDK loading | 23.1 MiB | 60.6 MiB |
| SDK loaded | 24.5 MiB | 61.1 MiB |
| SDK constructor returned, before first execution | 24.5 MiB | 61.0 MiB |
| Python guest initialized | 857.3 MiB | 879.4 MiB |
| Baseline snapshot retained | 1,327.7 MiB | 1,321.8 MiB |
| Guest holds a touched 1 MiB bytearray, before first restore | 1,328.9 MiB | 1,322.8 MiB |
| First restore complete | 1,119.1 MiB | 1,112.3 MiB |
| Guest holds a touched 16 MiB bytearray after a restore | 1,119.1 MiB | 1,111.8 MiB |
| Guest holds a touched 64 MiB bytearray after a restore | 1,119.1 MiB | 1,111.8 MiB |
| Final restore complete, guest payload absent | 1,119.1 MiB | 1,111.8 MiB |
| Python snapshot reference released | 1,119.1 MiB | 1,111.8 MiB |
| Sandbox released and garbage collection complete | 70.8 MiB | 62.4 MiB |

The maximum recorded application-container peak after the memory sequences was 1,781.61 MiB; the subsequent latency sequence raised the first container's lifetime peak to 1,783.28 MiB, or 1.74 GiB. `memory.peak` is a lifetime high-water mark: the memory probe followed a fresh-process startup in the same container, so the peak at an early memory point can include that preceding startup. The retained-snapshot point increased the peak beyond the earlier startup peak. Stage RSS/PSS samples and process peak RSS remain separately available in the results.

The idle holding container charged 0.65–0.70 MiB before any SDK process, and 44.04–44.16 MiB after the benchmark process exited, including retained file cache. These readings used a short-lived `cat` process. They exclude the completed init container, pod infrastructure, node services and container-runtime overhead, and are not the footprint of a running Python service. The host-Python row is a more useful starting point for that comparison.

Each guest bytearray was checked for its expected length and touched once per 4 KiB page. Restoring the baseline removed the variable each time. The 16 MiB and 64 MiB payloads did not increase RSS beyond the already resident memory after the first restore. This shows reuse within this measured memory configuration; it does not establish a maximum workload size or a general per-byte cost. Dropping the Python snapshot reference also did not immediately reduce residency. Snapshot capture increased RSS by about 470 MiB, but that difference is not a serialized snapshot size.

All five memory sequences reported zero `memory.events` counts, including limit hits, OOM and OOM kills. After normal sandbox release, no VM/vCPU descriptors remained. A separate diagnostic distinguished those descriptors from the SDK's cached device connection, which can remain open without retaining a VM. Abrupt owner death and container termination were not measured here.

## Reproduction and cleanup

Prepare the same scoped device-plugin and restricted application setup as the [execution proof](hyperlight-aks-kvm-verification.md#reproduction-and-restoration). Add `benchmark.py` to the proof ConfigMap. Keep the published pod's image, SDK requirements, security context, memory/CPU settings and heap/stack sizes unchanged for a comparable run. Use five fresh positive pods sequentially on the same selected node, applying the deny policy after each init installation. Run the `cold` mode before any other SDK probe in that pod so its private guest cache starts empty.

Within each prepared application container, execute the following commands with a 180-second deadline and a five-second kill allowance, retaining stdout, stderr and exit status. Run `cold` and `memory` in all five pods, and `latency` in the first pod only. Each command starts a separate Python process; all native objects within a command stay on its main thread.

```sh
timeout --signal=TERM --kill-after=5s 180s /work/venv/bin/python -I -u /proof/benchmark.py --mode cold
timeout --signal=TERM --kill-after=5s 180s /work/venv/bin/python -I -u /proof/benchmark.py --mode memory
timeout --signal=TERM --kill-after=5s 180s /work/venv/bin/python -I -u /proof/benchmark.py --mode latency --iterations 30
```

Require the final `complete` record, successful guest-result assertions, zero exit status and the expected source/image identities. The recorded benchmark SHA-256 is `4244e2484fbf96c8306fde2875ae51a8f705abc99ef91987fc9b5f512637bd04`. Raw results include successful VM/vCPU descriptor cleanup after repeated recreation and after the memory sequence. The initial pilot is excluded because its assertion counted the cached device connection as a live-VM descriptor; the corrected source above was used for every published sample.

Cleanup removed the five measured pods and the pilot, seed pod, plugin, helper and matching CDI state. The safeguard namespace exclusion was removed, final safeguard properties exactly matched the saved baseline, and the same plugin manifest was again refused by the original host-path admission policy. Both test namespaces were removed and the default workload pool returned to zero nodes. Pre-existing system resources remained.

These observations favor a warmed sandbox with baseline restore when calls require fresh variables under the same execution policy. They do not establish safe multi-tenant reuse, concurrent sandbox density, admission capacity, smaller heap/stack settings, OOM recovery, Linux adapter containment or production service latency. Those boundaries remain with [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228), [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) and the existing recovery/ownership follow-ups.
