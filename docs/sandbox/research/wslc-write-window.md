> Exploration and live measurements for [#1203](https://github.com/sokolaidev/maf-extensions/issues/1203): WSLC input placement authority and the check/copy boundary. The decision to retain an explicit residual is documented in the [backend contract](../backends/wslc.md#write-checkcopy-residual).

# WSLC write placement and parent swaps

Measured on 2026-09-13 with WSLC/WSL **2.9.4.0**, kernel **6.18.35.2-1**, Windows **10.0.26220.9223**, Python **3.13.12**, and maf-extensions at `190505961b0e19caf8b70b47f7a41246aa7c560b`. The non-root fixture is built from the checked-in [guest-owned Dockerfile](../../../packages/maf-sandbox-wslc/tests/fixtures/guest-owned/Dockerfile): Azure Linux core 3.0, uid/gid `10001:20001`, with a guest-owned working directory. The image used for measurement had ID prefix `68a4c6f59a37`. No Docker CLI measurement is substituted for a WSLC result.

## Deterministic interleaving

The [boundary tests](../../../packages/maf-sandbox-wslc/tests/test_wslc_write_window.py) run the backend's real classifier, tar builder and WSLC runner. Immediately before the stdin-to-container copy is submitted, a runner wrapper uses an exec **as the image user** to rename the checked parent and replace it with a link to `/protected`. This target is root-owned mode `0700`; a direct guest write fails with `Permission denied` before the measurement. Setup and result inspection use root, but the swap does not. Each case uses a separate network-isolated container, disposed in `finally`.

| Checked shape | Change immediately before extraction | WSLC result |
|---|---|---|
| Existing parent, write a file beneath it | Rename parent and link it to the protected directory | Copy succeeds; file lands under `/protected`, outside the working directory |
| Existing parent, create a missing child and file | Same swap | Copy succeeds; new guest-owned directory and file land under `/protected` |
| Missing parent included as a tar directory entry | Plant a link at that exact missing path | Copy succeeds; that entry replaces the link with a directory; this interleaving does not redirect the file |
| `prepare_work_dir`, existing parent and missing child/base | Rename parent and link it to the protected directory | Copy succeeds; intermediate directory lands root-owned under `/protected`, base lands with guest ownership |
| `prepare_work_dir`, missing parent included in archive | Plant a link at that exact missing path | Entry replaces the link; this interleaving does not redirect creation |

The protected directory remains `0:0:700`; escaped files are `10001:20001:644`, and the new base is `10001:20001:755`. These results establish **root-equivalent placement authority independently of tar ownership**. They do not establish host-filesystem escape: the protected target is inside the container. Replacement of a link at an explicit directory entry is not a closure proof: an existing prefix is still re-resolved, and the guest can mutate the filesystem while extraction runs.

Planting the link **before classification** is refused without submitting an input archive. Cancelling at the check/copy boundary likewise submits no archive and leaves the container usable. Offline tests also cover cancellation during classification and during the copy await, and an engine extraction error. Cancellation after submission cannot promise rollback or absence of partial files; killing the host CLI is not a transaction abort in the engine.

There is no unpaused statistical race gate here. All five shapes use an explicitly ordered swap, and all five also exercise pre-check refusal and pre-submission cancellation. These are characterization tests of the stated residual; a future closure should change their expected outcomes.

Reproduce from the repository root in PowerShell:

```powershell
wslc image build -t maf-sandbox-wslc-guest-owned:ci packages/maf-sandbox-wslc/tests/fixtures/guest-owned
$env:MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE = 'maf-sandbox-wslc-guest-owned:ci'
uv run pytest packages/maf-sandbox-wslc/tests/test_wslc_write_window.py -q
```

## Available mechanisms and their cost

The installed CLI help and upstream source were checked separately. Upstream `master` was [`eaa69e766cf375d96053207a4ba8858f54ea1536`](https://github.com/microsoft/WSL/tree/eaa69e766cf375d96053207a4ba8858f54ea1536); it is source evidence, not a live test of that revision. [`ContainerCpCommand::GetArguments`](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/wslc/commands/ContainerCpCommand.cpp) exposes no user or no-follow selector. [`WSLCContainerImpl::UploadArchive`](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/wslcsession/WSLCContainer.cpp) calls the internal runtime's `PutArchive` with the container ID and destination string. Its shared object lock does not span the backend's earlier checks or prevent guest filesystem mutations.

| Candidate | Evidence and cost | Decision |
|---|---|---|
| Trusted constrained upload / held resolution | No base-relative no-follow upload exposed by the inspected CLI or [COM interface](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/service/inc/wslc.idl) | Request upstream; unavailable to this backend today |
| Guest freeze while copying | No pause/unpause command in installed help or upstream commands, nor a freeze method or paused state in the [public SDK](https://github.com/microsoft/WSL/blob/eaa69e766cf375d96053207a4ba8858f54ea1536/src/windows/WslcSDK/wslcsdk.h) | No supported operation to time or adopt; no thaw/recovery implementation is claimed |
| `container kill --signal SIGSTOP` / `SIGCONT` | Both returned 0, but a new guest exec between them returned `still-running`; SIGCONT and disposal completed | Process signalling does not freeze the container; not an alternative to a freezer |
| Stop/start | Ends and restarts workload processes, losing running exec state; the remaining classifier also requires guest exec | Changes lifecycle semantics, not an acceptable transparent file-operation guard |
| Transfer through guest exec | Can bound placement to the image user, but requires a trusted helper or extra utilities; the shared shell route needs `sh`, `base64`, `mkdir`, `mv` | Additional image contract and transfer cost; not adopted for this engine-tar file plane |
| Keep engine tar and state the residual | No new image dependencies or extra engine round trips | Selected; this states the risk rather than reducing placement authority |

Two exploratory passes measured input-copy subprocess durations of roughly **35–163 ms** and whole checked operations of **0.29–0.93 s**, including the deterministic swap exec. These small local samples are descriptive, not performance guarantees. No freeze timing exists because no supported freeze operation was found. No guest-helper benchmark was performed because that transport was not selected.

Even a future freeze command is insufficient by itself for the current algorithm: classifying an accepted non-directory copy source still executes guest `test`, which cannot be assumed to run under a real freeze. It also needs trusted metadata that can be read while frozen. The output-archive request [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) may supply type metadata but cannot alone hold resolution through upload. Bypassing WSLC to reach its internal runtime is not a supported WSLC API contract.

## Decision and remaining work

Retain `FILES_IN` with a prominent, measured residual beside the capability declaration, including `prepare_work_dir` on cold acquire and warm repair. Host-side serialization alone cannot stop guest background processes. Non-root archive ownership is not a bound, and neither a second stat nor a guest shell check closes the interval. Workloads requiring confinement against concurrent guest mutation must use a backend with a supported closure or avoid this input plane.

The focused upstream request is for **constrained archive upload with held, no-follow resolution**. Publication and its issue link remain pending under [#1203](https://github.com/sokolaidev/maf-extensions/issues/1203). A freeze-based alternative would require engine-authenticated metadata during freeze and the concurrency, exec, cancellation, failed-thaw and warm-recovery requirements recorded in [#1130](https://github.com/sokolaidev/maf-extensions/issues/1130). No such lifecycle implementation or live freezer validation is claimed by this record.
