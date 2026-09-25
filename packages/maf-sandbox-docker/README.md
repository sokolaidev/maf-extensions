# maf-sandbox-docker

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxDockerExperimentalWarning`.

Run sandbox workloads in Linux containers through the Docker CLI. This backend provides commands, file transfer and guest-to-host tool calls. Its Python dependency is `maf-sandbox`.

This is an independent package, not a Docker Inc. or Microsoft product.

## Quickstart

```bash
pip install maf-sandbox-docker
```

```python
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

backend = await DockerSandboxBackend.create(DockerSandboxConfig())
router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
```

The async factory reads the daemon's OS and declares POSIX for Linux. The plain constructor also works, but declares no OS family. A kind requiring POSIX therefore needs the async factory.

Container isolation shares a kernel and is below the router's default microVM minimum. The explicit minimum above allows it. Docker Desktop's shared VM does not change this declaration.

See the [Docker CodeAct sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/06_docker_codeact) or [file-output sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/08_docker_codeact_files) for a complete application.

## Requirements

Use Docker Desktop or Docker Engine with a reachable Docker CLI. Both rootful and rootless Linux engines are supported. Windows requires an event loop that can start subprocesses, such as the default Proactor loop.

`DockerSandboxConfig.docker_path` selects the CLI binary. The client must support Docker context inspection; the Podman CLI does not. Other compatible engines reached through Docker's CLI are not verified here.

The backend captures the client environment and binds its context, endpoint and TLS settings. Later context changes cannot redirect it. A missing context refuses instead of falling back. The async factory binds immediately; the constructor binds on first use.

## Supported operations

| Setting | Behavior |
|---|---|
| Isolation | `CONTAINER` |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_DELETE`, `HOST_TOOLS`, `RECLAIM` |
| Network | `CLOSED`; `ALLOWLIST` with a configured proxy |
| Lifetime | Conversation or separate sandbox per call |
| Transfer ceiling | 64 MiB per file, 256 MiB total, 256 files in each direction |
| Command output | 8 MiB of stdout and stderr together; more refuses the call and discards the container |
| Cleanup | Disposal by default; reclaim requires explicit host opt-in |

Directory listing, runtime `run_code`, snapshots and core attached identity are unavailable. Method and path network rules require the configured proxy.

Acquisition checks the guest commands needed by the requested capabilities. `EXEC` needs `sh`, even for an argv-only workload. Deletion needs `rm`; host tools also need `mkdir`, `mv` and `nohup`. File transfer itself needs no guest command.

The backend adds no host bind mount or Docker socket. Every container uses `no-new-privileges` and a PID limit. Dropping all capabilities, memory limits and CPU limits are optional configuration.

## File transfer

Acquisition prepares the storage base for workloads using commands or files. `work_dir=None` selects `/maf-sandbox/work`; an explicit path requests that exact base. Use `working_directory="."` to address it.

Files move through Docker's tar-based copy API. Input files and newly created directories use the resolved image UID/GID. Existing directories keep their contents, ownership and modes.

Output reads check every ancestor from the filesystem root and reject links or non-directory parents. The final entry must be a regular file. Reads over the caller's cap fail without returning a prefix.

The container stays paused across each path check and transfer. This prevents guest code from replacing a checked parent before the copy. It also stops all guest execution during the transfer and adds overhead, especially when host tools poll for files.

The engine must support pause, including for ordinary command acquisition that prepares a work directory. There is no unpaused fallback.

Transfers observe the ordinary container filesystem. Keep outputs out of tmpfs, `/proc`, `/sys`, `/dev` and other guest mounts the copy API does not expose. A guest-visible mounted file can appear absent to the collector.

`FILES_LIST` is unavailable because Docker's directory archive transfers the whole subtree to discover its names. Kinds must name outputs explicitly.

## Image users

The backend resolves `Config.User` using container account files and, when needed, `id`. An unset user means root. A numeric `uid:gid` is the clearest image setting.

Unresolved users refuse workloads requiring `FILES_OUT` or `HOST_TOOLS`. Root-owned inputs cannot promise the guest can create adjacent outputs or transport files. Other workloads may proceed with a warning and root-owned inputs. Failed resolution is retried on later acquisition.

## Network access

Without a proxy, only `Egress.CLOSED` is supported and containers use `--network none`. An allowlist request is refused.

Build the packaged proxy once and select it in configuration:

```python
from maf_sandbox_docker import DockerSandboxConfig, proxy_build_context

print(f"docker build -t maf-egress-proxy:local {proxy_build_context()}")
config = DockerSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
```

A nonempty allowlist needs Docker Engine 28.0.0 or newer. The workload joins an isolated internal network with no bridge address. Its only route out is a filtering proxy that permits the spec's hosts. Proxy environment variables help clients find it; the network topology enforces the restriction.

The packaged image builds pinned iron-proxy with a policy patch. It terminates guest TLS to check the host, method and path, validates upstream certificates, and supplies a per-sandbox CA certificate through the guest work directory. Its CA key stays in the proxy. Clients must honor the injected CA environment or configure trust explicitly. Public destinations require TLS on every port. Private destinations require TLS by default; `DockerSandboxConfig(allow_private_http=True, egress_proxy_image=...)` permits plaintext only when the listed host resolves to a private address. Use that option only for development or test workloads. Unrestricted access remains unavailable. An empty allowlist uses `--network none`.

Proxy decisions can be reported through the router's observer after confirmed proxy removal. Failed removal can leave a window unreported; missing events do not prove no traffic occurred. See [egress observation](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md).

## Cleanup and retention

Acquisition reuses a matching running container, restarts a stopped one or creates a missing one. Router-managed tools dispose after each call unless the host explicitly permits reclaim. Reclaim can leave state outside the call directory; a kind's confinement declaration is advisory.

`dispose(key, kind=...)` removes a selected kind. `dispose_scope(scope, thread_id)` finds conversation resources through engine labels, including resources created by another host process.

An operator can remove old workloads and orphaned proxy infrastructure:

```python
from datetime import timedelta

result = await backend.reap(timedelta(hours=24), scope="my-app")
print(result.disposed, result.proxies_removed, result.networks_removed)
for failure in result.failures:
    print(failure)
```

**This is a maximum creation age, not an idle timeout. It can terminate active sandboxes.** Omitting `scope` covers all backend-owned scopes on the engine. The backend starts no scheduler.

Inventory and revalidation failures prevent deletion. Removals target inspected resource IDs, and attached networks are not forcibly disconnected. Individual failures remain in the result for operator handling. See [cleanup ownership](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/operations.md).

## Verification

The repository's Docker live suite runs shared conformance probes against a real engine. To test a kind's confinement claim, use `DockerFingerprintSubject` with `assert_nothing_left_behind` and require every result to pass.

```python
from maf_sandbox.conformance import assert_nothing_left_behind
from maf_sandbox_docker.conformance import DockerFingerprintSubject

subject = DockerFingerprintSubject(sandbox, observer_image="trusted-python-observer:local")
results = await assert_nothing_left_behind(subject, call_and_cleanup)
assert all(result.passed for result in results)
```

`call_and_cleanup` runs the kind and awaits its cleanup on a fresh sandbox. Set `MAF_SANDBOX_DOCKER_E2E_IMAGE` and `MAF_SANDBOX_DOCKER_OBSERVER_IMAGE` to run the repository's live observer tests with your images.

A backend-provisioned proxy CA is verified against the current trusted proxy on every observation. CA rotation is allowed; altered certificate bytes, permissions, ownership, extended attributes and unrelated residue fail the probe. The CA and its ancestor directories are measured separately from the root filesystem diff.

The subject needs a trusted local observer image with Python 3.12 or newer. It measures final filesystem and process state, including mounted storage such as `/dev/shm`. A restored temporary change can leave no measured residue, and the probe does not prove all kernel state is clean.

See the [backend guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/docker.md) for measurement limits and the complete transfer, network and cleanup contract.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
