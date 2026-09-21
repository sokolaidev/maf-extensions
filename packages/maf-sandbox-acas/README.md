# maf-sandbox-acas

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxAcasExperimentalWarning`.

Run workloads in microVM-isolated Azure Container Apps Sandboxes. This backend supplies commands, input and output files, directory listing and guest-to-host tool calls.

This is an independent package for [Microsoft Agent Framework](https://aka.ms/AgentFramework), built on the [Azure Container Apps Sandboxes preview](https://learn.microsoft.com/azure/container-apps/sandboxes-overview). It is not a Microsoft product.

## Quickstart

```bash
pip install maf-sandbox-acas
```

```python
from maf_sandbox import SandboxRouter
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

backend = AcasSandboxBackend(
    AcasSandboxConfig(
        endpoint="https://management.<region>.azuredevcompute.io",
        subscription_id="<sub-id>",
        resource_group="<resource-group>",
        sandbox_group="<sandbox-group>",
        registry="<registry>.azurecr.io",
    )
)
router = SandboxRouter([backend])
```

The backend meets the router's default microVM minimum. The isolation boundary is provided by the Azure service.

Authentication defaults to `DefaultAzureCredential`. The SDK is pinned to `azure-containerapps-sandbox==0.1.0b4`; the adapter needs its tested byte-capture and file-metadata interfaces.

See the [Bicep sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) or [CodeAct sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/03_acas_codeact) for caller context, tool wiring and teardown.

## Images and capabilities

| Image setting | Resolution |
|---|---|
| Bare name, such as `python-3.13` | The service's prebuilt image catalogue. |
| Tagged reference, such as `bicep-sandbox:0.46.1` | A disk image already imported into this sandbox group; `registry` qualifies short references. |
| `image_id` | A pinned service disk-image ID; no reference lookup. |

Unknown bare names are refused. Tagged references must be imported before use. The [scripts guide](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-acas/scripts/README.md) covers import and lifecycle recovery.

| Setting | Value |
|---|---|
| Isolation / guest | `MICROVM` / POSIX |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `HOST_TOOLS` |
| Network | `CLOSED` or host `ALLOWLIST` |
| Lifetime | Conversation or separate sandbox per call |
| Transfer ceiling | 32 MiB per file, 128 MiB total, 128 files in each direction |
| Cleanup | Disposal; no reclaim or snapshot reset |

Capabilities also depend on the image. Acquisition checks byte-capture utilities and guest removal behavior. Missing prerequisites refuse the requested capability. Output collection and host tools can be refused when the guest cannot remove a probe file beside uploaded files.

Command capture needs `sh`, `mkdir`, `mkfifo`, `head`, `cat`, `wc`, `dd`, `base64`, `rm`, `rmdir` and writable `/tmp`. Host tools also need `mv` and `nohup`. The workload supplies its own interpreter or compiler.

## File authority and limits

Acquisition prepares the storage base for workloads using commands or files. `work_dir=None` selects `/maf-sandbox/work`; an explicit path selects that exact base. Service-created directories may be root-owned. Existing directories keep their contents, ownership and modes.

**Writes always run as the guest.** The image needs a guest-writable directory and the shell transfer utilities. A permission failure has no privileged file-API fallback. Bake the writable base into a non-root image rather than assuming acquisition will grant access.

The transfer stages base64 chunks and renames the complete file into place. `read_timeout_seconds` bounds the whole transfer. Larger files cost more guest commands and can time out before reaching the declared byte ceiling.

`remove` also runs as the guest, then checks absence through the file API. The path checks and guest command are separate; a changed parent can redirect the operation within the guest's existing permissions.

<a id="native-reads-retain-a-confinement-residual"></a>

### Native reads retain a path race

Reads, stat and listing use the service's file API with host authority. They reject links observed during checks, but cannot hold those paths unchanged through the operation.

| Method | A concurrent replacement can expose |
|---|---|
| `read_file` | Bytes outside the checked directory, including bytes the guest cannot read. |
| `stat_file` | Metadata outside the checked directory. |
| `list_dir` | Names and metadata from another directory. |

The service's file flags also cannot reliably distinguish a FIFO from an empty regular file. A bounded read timeout prevents an endless wait; it does not establish that the entry is regular.

Byte caps, timeouts, root images and disposal do not close the path race. Select a backend with a held filesystem boundary when that guarantee is required. See the [file contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/acas.md#native-reads-retain-a-confinement-residual).

## Network and identity

The spec supplies network policy. The backend applies default deny and one allow rule per permitted host. It provides no method-level enforcement declaration or egress observations.

A warm instance keeps its original network policy. Changing mode or hosts raises `AcasEgressPolicyConflict`. Coordinate active calls and dispose that kind successfully before changing policy, or use another key.

Managed identity configured on the sandbox group is deployment-owned authority. The host must route workloads to groups with the intended permissions. The adapter performs no ARM assignment discovery and does not apply core `ATTACHED_IDENTITY` opt-in or retention checks to that identity.

The host's SDK credential remains outside the guest. Deleting sandboxes does not revoke group principals or previously issued tokens.

## Host-selected credentials

Set `credential_resolver` to an async callback returning `AcasCredentialBinding(authority, generation, create_credential)`. Its request identifies acquire, disposal or conversation purge and the trusted scope and conversation.

Acquire may use a request grant. Disposal must independently recover a cleanup grant, including on another host replica. Resolver failure never falls back to default identity.

The factory creates a fresh async credential on its owning loop; the backend owns closure. Shared singleton credentials are unsupported. Equal authority and generation values assert equivalent grants. Use a new generation when authentication state changes.

| Setting | Default |
|---|---|
| `max_clients_per_loop` | 32 |
| `client_wait_seconds` | 30 seconds |
| `client_close_seconds` | 30 seconds |

Capacity applies per host replica and event loop. Call `await backend.aclose()` before stopping owner loops. It permanently refuses new work and raises `AcasClientCloseError` if SDK cleanup is incomplete. It closes clients; it does not delete sandboxes.

See the [credential guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/acas-credentials.md) for host wiring and grant rotation. Offline tests do not verify Azure's acceptance of a particular delegated token.

## Execution and cleanup

Core disposes after each sandboxed tool call. Direct callers must arrange disposal too. `dispose(key, kind=...)` reaches locally known instances; `dispose_scope(scope, thread_id)` queries service labels and can find another replica's sandboxes.

Stop new work across replicas before conversation deletion. Local acquisition and purge guards do not coordinate other processes. Retained deletion failures refuse acquisition until cleanup succeeds; incomplete purges need retry.

The backend configures service auto-suspend and auto-delete on new sandboxes. These timers supplement host cleanup. See the [lifecycle contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/acas.md#lifecycle) for configuration failure and retention limits.

`ExecResult` preserves program bytes in `stdout_bytes` and `stderr_bytes`; text views use UTF-8 replacement decoding. After complete capture, a reported scratch-removal failure warns and returns the captured result. Scratch may remain until disposal.

Incomplete capture, exceptions, cancellation and execution timeouts invalidate and attempt to dispose the sandbox. A deadline during an observed HTTP 429 `Retry-After` sleep can retain it because no retry started. That exception does not cover direct cancellation or other retry statuses.

The [backend guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/acas.md) covers probes and failure behavior. The [live service suite](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-acas/tests/test_acas_e2e.py) requires Azure credentials and configured images; it is separate from ordinary offline checks.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
