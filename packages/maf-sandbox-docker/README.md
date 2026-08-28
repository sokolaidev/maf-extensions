# maf-sandbox-docker

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxDockerExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_docker.MafSandboxDockerExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Docker Inc. or Microsoft — it is a third-party sandbox backend for [Microsoft Agent Framework](https://aka.ms/AgentFramework).

```
app  ->  maf_sandbox  ->  maf_sandbox_docker  ->  the container
```

The sandbox backend for everyone `wslc` leaves out: plain Docker containers, driven through the `docker` command-line client, on any machine with a Docker-compatible engine — macOS, Linux, Windows with WSL 2, and every GitHub Actions `ubuntu-latest` runner. No subscription, no login, and no dependency but [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox) itself. A workload written against the protocol runs here unchanged, which is what makes it a workload rather than an integration.

## Quickstart

```bash
pip install maf-sandbox-docker
```

```python
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

router = SandboxRouter([DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER)
```

[`samples/06_docker_codeact`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/06_docker_codeact) runs those two lines end to end: an agent that executes model-written Python in a container and reads the result back out. Its siblings `03_acas_codeact` and `04_wslc_codeact` are the same program on a microVM-isolated Azure backend and on `wslc`, and the diff between any two of them is two imports and one constructor.

## Requirements

**A Docker-compatible engine, reachable through the `docker` client.** Docker Desktop (macOS, Linux, Windows with WSL 2) and Docker Engine (Linux, rootful or rootless) are what this backend supports. The client's own configuration — `DOCKER_HOST`, the active context, TLS settings — is inherited, because every call is a subprocess that inherits this process's environment; point `DockerSandboxConfig.docker_path` at a different client binary to use another one. Colima, OrbStack, Rancher Desktop and Podman expose Docker-compatible sockets and may work through the same client (Podman's default outbound network is called `podman`, so set `outbound_network="podman"` in allowlist mode), but they are not officially supported and nothing here is verified against them.

Every call spawns the `docker` client, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so.

**Hosts this backend does not serve:** Windows without WSL (Docker Desktop's Hyper-V backend is documented by Docker but not its default, needs Pro or Enterprise, and is not verified here; Windows Home has no route at all), GitHub Actions' `windows-latest` (Windows containers only) and `macos-latest` (no Docker, no nested virtualization). For WSL-less Windows the eventual answer is a separate backend over Docker's "Docker Sandboxes" micro-VM product.

## What this backend declares

**`Isolation.CONTAINER`.** A container shares the host kernel, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — construct the router with `min_isolation=Isolation.CONTAINER` and it admits this backend; leave the floor at its default and construction raises `SandboxBackendNotPermitted`. A Docker Desktop or Colima VM does not lift the rung: one shared VM kernel serves every container, the same shape `wslc`'s WSL 2 utility VM has, and the ladder classifies that at `container`. The declaration is a **constant** — no configuration raises it, because a security level the backend cannot verify must not become one the router repeats.

**`egress_modes = {closed}` by default, `{allowlist, closed}` with a proxy configured.** With no proxy configured every container is created `--network none`: a network namespace with only loopback, enforced by whichever kernel runs the container. That serves a workload declaring `Egress.CLOSED`, and **refuses** one declaring `Egress.ALLOWLIST` — the router never substitutes a mode, so denying everything is no longer offered as a stricter stand-in for a host list. Never `UNRESTRICTED`: a container backend always cuts or proxies, so it cannot serve a workload that asked to run open.

Set `egress_proxy_image` and `ALLOWLIST` joins the set: each sandbox gets its own internal network and a dual-homed filtering proxy, and the spec's allowlist is enforced by topology — the container has no route out except the proxy, which opens a CONNECT tunnel only to the hosts the spec names. The `HTTP_PROXY`/`HTTPS_PROXY` variables set on the workload are how ordinary clients find the proxy, not what enforces the allowlist; the topology is. TLS is not decrypted, and the sandbox never resolves an external name itself. The proxy is shipped as source, not as an image you must trust: build it from the packaged recipe, whose only pinned dependency is its Azure Linux base.

```python
from maf_sandbox_docker import proxy_build_context, DockerSandboxConfig

print(f"docker build -t maf-egress-proxy:local {proxy_build_context()}")  # run this once
config = DockerSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
```

**`Capability.FILES_OUT`, never `Capability.FILES_LIST`.** This backend reads declared outputs back out — `docker cp <container>:<path> -` streams a tar whose first 512-byte header carries the size, the entry type and any link target, so a file is statted and read from one stream with no stat command and no shell in the image. It does **not** enumerate directories: Docker has no engine-level primitive for it, which is exactly why the protocol splits enumeration into `FILES_LIST`. A kind that cannot name its outputs in advance requires that capability and is refused here — served instead by a backend, like ACAS, that has native listing.

**Every path component is checked, not just the last one.** A symlink is refused on the tar entry's type bit only when it is the entry being tarred; the engine resolves the path daemon-side, so a guest that points `out` at `/etc` gets a stat of `out/hostname` describing a regular file with the parent link nowhere in it. `stat_file` and `read_file` therefore stat every parent component from the **filesystem root** down — not from the working directory, whose own ancestors the guest can replace just as easily: with `/maf-sandbox -> /` unchecked, `/maf-sandbox/work` stats as a real directory and serves `/`. The check itself is `maf_sandbox.paths.refuse_symlinked_parents`, not a copy living here: this backend passes it the unconfined tar-header stat above. A link is refused as a *confinement* failure and any other non-directory as an ordinary `ENOTDIR` — the entry comes back as `EntryKind.SYMLINK` or `EntryKind.OTHER`, so a caller can tell an escape from a guest tripping over its own fifo. One residual stays open: the check and the read are separate calls and `docker cp` has no no-follow form, so a guest that swaps a stat-ed component for a link in between is followed.

Whether that is actually enforced is not this package's own claim either. `maf_sandbox.conformance` is the shared suite every backend serving `FILES_OUT` answers, and this is the one backend that answers it **against a real engine on every pull request** — a container on the runner, a hostile layout planted in it through the public surface, and the probes attacking that.

## The backend

`DockerSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent, kind)`. A running container is reused, a stopped one started, a missing one created; an absent image is pulled explicitly first so a cold pull does not ride the lifecycle timeout |
| `write_file(path, content, *, working_directory)` | a confined one-entry tar on stdin to `cp - <container>:/`, which creates the parent directories from the entry name; `str` is UTF-8, `bytes` is written as given |
| `stat_file` / `read_file` | the `FILES_OUT` pull surface — stat from the first tar header of `docker cp`, read from the same stream; symlinks and other non-regular entries refused on the header type, every parent component refused unless it is a real directory, a body over the caller's cap refused rather than truncated |
| `dispose(key)` | `rm -f` on every kind's container the key names, with the proxy and network of an allowlisted one |
| `dispose_scope(scope, thread)` | delete every container for a conversation — **by label, read back from docker**, not from process memory |
| `isolation` | `container`, unconditionally |
| `egress` | `closed`, or `allowlist` when `egress_proxy_image` is set |
| `capabilities` | `{EXEC, FILES_IN, FILES_OUT, FILES_DELETE, HOST_TOOLS}` |
| `limits` | the transfer ceilings a spec may not exceed, per direction |

Container names are derived from the key and kind rather than remembered, so `acquire` and `dispose` agree on one without a registry to keep in sync. Labels are the durable record `dispose_scope` selects on, and their values are digested when they are long or carry a separator — the same mapping on both sides, because transforming one and not the other makes a purge quietly select nothing.

No bind mounts, no host paths, and never the Docker socket cross into a sandbox — files go in and out only through `docker cp`. The hardening flags `--security-opt no-new-privileges` and `--pids-limit` go on every container; `--cap-drop ALL`, `--memory` and `--cpus` are opt-in through the config.

`stop` is never used. A container whose init process ignores `SIGTERM` takes ten seconds to stop and a fraction of a second to remove, and there is nothing in a sandbox worth waiting for.

## Upgrading to 0.7

`0.7.0` requires `maf-sandbox` 0.19, which made the egress mode a thing a workload declares and a set a backend enforces.

**`egress` is replaced by `egress_modes: frozenset[Egress]`.** A host that read `backend.egress` gets an `AttributeError`; read `backend.egress_modes` instead. Nothing in the wiring changes — the set is still derived from `egress_proxy_image` exactly as the single value was.

**Without a proxy image this backend now refuses an allowlist workload rather than confining it further.** That is the upgrade's one behavioural break, and it is most likely to reach you through a kind whose default asks for one — `maf-sandbox-bicep` 0.9 does:

```
SandboxEgressNotEnforced: sandbox backend 'docker' cannot enforce the 'allowlist'
egress the 'bicep' workload runs in (it enforces closed).
```

Configure `egress_proxy_image` if the workload is meant to reach the hosts it names, or ask the kind for `Egress.CLOSED` if it is meant to run offline. The old behaviour — serve it anyway, warn, and let the workload fail at the first fetch — is gone deliberately: a fetch failure deep in a tool call is a worse report than a refusal at attach.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
