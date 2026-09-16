# Docker backend research

> Consolidated research record for the plain-Docker backend, 2026-09-09 through 2026-09-14. It combines the backend exploration, implementation proposal and directory-listing measurements. The backend is now documented as a decided design in [`../backends/docker.md`](../backends/docker.md); this record retains the reasoning, measurements and boundaries that led there.

## Decision at a glance

Add `maf-sandbox-docker` as a backend that drives a Docker-compatible CLI against a Docker-API-compatible socket. It runs Linux containers on a developer machine or Linux CI runner, declares `Isolation.CONTAINER`, uses `EXEC`, `FILES_IN` and named-file `FILES_OUT`, and withholds `FILES_LIST`. Egress is `CLOSED` by default or `ALLOWLIST` through an internal-network plus dual-homed CONNECT proxy. The backend adds no runtime dependency beyond `maf-sandbox`.

This is deliberately not Docker Sandboxes, Docker's separate micro-VM product. Docker Sandboxes deserves a distinct `maf-sandbox-docker-sbx` backend at the `MICROVM` rung because it has a different boundary, CLI and lifecycle. A configuration flag must not change the isolation claim of the plain-container backend.

The backend closes the local/CI gap left by ACA Sandboxes and WSLC: macOS, Linux and Windows with WSL can run a real sandbox without an Azure subscription, while `ubuntu-latest` can run live backend tests using its preinstalled Docker engine. Windows without WSL is deferred because the Hyper-V route is not the default and is not validated in this repository.

## What the contract means

### Engine contract and supported hosts

The backend targets the command-line contract rather than Docker Inc.'s daemon specifically. Docker Desktop, native Docker Engine and Docker-compatible sockets can satisfy the contract, but Docker Desktop and Docker Engine are the supported engines; Podman, Colima, OrbStack and Rancher Desktop remain best-effort until measured.

| Host | Expected engine shape | Boundary |
|---|---|---|
| macOS | Docker Desktop, Colima, OrbStack, Rancher Desktop or a VM-backed compatible engine | Containers share the engine VM's kernel |
| Linux | Docker Engine rootful/rootless or a compatible socket | The daemon kernel is the host kernel |
| Windows with WSL 2 | Docker Desktop WSL backend | Containers run in the WSL utility VM |
| Windows without WSL, Pro/Enterprise | Docker Desktop Hyper-V backend | Documented but not validated or default |
| Windows Home without WSL | None in the supported scope | Hyper-V is unavailable and the backend does not install WSL |
| Windows containers mode | Out of scope | The backend targets Linux containers |

The client inherits `DOCKER_HOST`, the selected Docker context and the client's other environment-based connection settings. The backend must not reimplement socket discovery when the real CLI already handles it. The current adapter documentation also captures context binding and daemon OS-family checks; this record's original exploration predates those implementation details.

### Honest declarations

`isolation` is the constant `Isolation.CONTAINER`. A Docker Desktop or Colima VM does not lift a container to `MICROVM`: one shared VM kernel serves every container, so the boundary between sibling sandboxes remains namespaces and cgroups. A higher runtime such as gVisor would be a different backend declaration only after an independent verification story; an unchecked config string must never become a security guarantee.

The initial capability ceiling is `EXEC`, `FILES_IN` and named-file `FILES_OUT`, with no `FILES_LIST`, `RUN_CODE` or general `NETWORK` declaration. The current backend guide records later additions such as deletion, host tools and reclaim; those are implementation-specific declarations, not reasons to make the original research claim broader. `FILES_LIST` remains withheld because Docker has no cheap engine-level name enumeration.

Egress is config-derived: without a proxy image the backend declares `CLOSED`; with a filtering proxy image it declares `ALLOWLIST` as well. A spec that allows no hosts still uses `--network none` even when a proxy is configured. The declaration describes what the configured backend can enforce, not what a particular call happens to request.

The container-rung backend sits below the router's default `MICROVM` floor, so hosts opt down explicitly with `min_isolation=Isolation.CONTAINER`. Registering Docker beside ACA does not provide per-workload selection until router selection supports it; `selected="docker"` is the explicit current route.

## Why the CLI subprocess driver was chosen

Everything in the protocol is asynchronous. The candidate drivers were:

| Driver | Assessment |
|---|---|
| `docker`-py | Mature but synchronous, adds an HTTP dependency and requires thread offload; its exec timeout, exit-code, stream-separation and large archive behavior were not reliable enough for this backend |
| `aiodocker` | Natively async but adds `aiohttp`, makes socket/context/version handling the package's responsibility and carries more dependency weight than the backend needs |
| `python-on-whales` | A synchronous wrapper around the same CLI subprocesses; useful prior art, but redundant |
| Docker CLI through `asyncio.create_subprocess_exec` | Preferred: no runtime dependency, native async supervision, argv without shell interpolation, and the real CLI already handles contexts, `DOCKER_HOST`, Docker Desktop, native Engine and compatible sockets |

The backend follows WSLC's private subprocess seam: construct an argv list, run it with `asyncio.create_subprocess_exec`, bound it with `asyncio.wait_for`, and kill/reap children on timeout, cancellation or failure. The Docker seam is bytes-native because tar streams must not be decoded with replacement characters. A configurable client path remains an escape hatch, not a compatibility promise. A missing or stopped daemon should produce an actionable acquire-time error naming the client and preserving the engine message.

The CLI contract needed by the backend is small: lifecycle operations, `exec` with working directory and argv, tar streaming through `cp`, labels and label filters, `none` and user-defined/internal networks, hardening flags and cgroup limits. Output formats and compatibility claims vary across engines, so listings must be parsed defensively and real-engine tests must pin captured formats.

## Lifecycle and containment shape

Containers are named from the complete sandbox identity: scope, thread, agent, kind, egress identity and optional call ID. Labels carry the same identity and are the durable source for disposal and purge. A per-event-loop acquire lock handles local get-or-create races; name-conflict adoption handles races across processes. Acquire reuses a running container, starts a stopped one, creates an absent one or adopts a concurrent winner. Egress networks and proxies are revalidated or rebuilt on every acquire instead of trusting stale scaffolding.

Purge selects by labels so a conversation delete can reclaim containers created by another replica. It removes workloads, proxies and networks best-effort and never raises from teardown. An age-based operator cleanup can inspect creation times and identity labels without the dead process's registry; it is an operator policy, not an automatic scheduler.

The create command is conceptually:

```text
docker run -d --name <derived-name> --security-opt no-new-privileges --pids-limit <limit> [--cap-drop ALL] [--memory ...] [--cpus ...] [--network none | --network <internal-network>] -l <identity-labels> <image> sleep infinity
```

The design uses no bind mounts, host paths or Docker socket passthrough. Files enter and leave through tar streams, and the container's image/user determines guest-side ownership. `--security-opt no-new-privileges` and a PID limit are baseline hardening; `--cap-drop ALL`, memory and CPU limits require live image evidence before becoming defaults. Rootless mode and user-namespace remapping are installation properties the backend does not promise.

A timed-out `exec` discards the sandbox because killing the CLI client cannot reliably kill the process inside the container and there is no portable per-command handle. Cancellation reaps the client while leaving the sandbox alive. Warm reuse remains the default for conversation scope; call scope includes the call ID in names, labels and registry keys and pays a cold create per call.

## File channels

### Named files in and out

`FILES_IN` uses a one-entry tar sent to `docker cp - <container>:/`, with the full relative destination in the tar entry name. The root destination always exists, so intermediate directories can be created without bind mounts or host-path translation. The tar carries the intended uid/gid; placement authority remains the daemon's, so the backend must not confuse ownership metadata with confinement.

Named-file `FILES_OUT` uses the same tar stream in reverse. Docker's first tar header carries size, entry kind and link target, allowing a stat-before-read contract without a shell or an additional HTTP client. `stat_file` refuses unknown or over-cap sizes before content moves; `read_file` streams only within the declared limit and kills/reaps the child on a breach. Symlinks and other non-regular entries are refused both from metadata and from the tar type flag. Every parent component is checked for symlinks and non-directories before the copy.

The check and copy are separate daemon operations. The design therefore freezes the guest around `prepare_work_dir`, `write_file`, `read_file` and `stat_file` with `docker pause`/`unpause`, so a guest cannot replace a checked parent between the path check and the daemon copy. A second file call cannot freeze the same container concurrently. Pause support becomes an acquire prerequisite even for ordinary `EXEC` specs whose working directory requires preparation. Failed thaw/recovery refuses reuse; removal and reclaim are bounded by their own reach rules instead of running while paused.

### Why `FILES_LIST` remains withheld

Docker can archive a directory, but the archive recursively streams file bodies between headers. It has no depth-limited, headers-only or paginated listing operation. Discovering ten names can transfer a gigabyte if an unrelated descendant contains ten 100 MiB files. A byte ceiling must refuse an oversized archive rather than return a partial listing. Guest-controlled `ls` or `find` is not an engine observation and cannot substitute for the capability.

The measurement used Docker Client/Engine 29.7.2, API 1.55, a Windows client and Docker Desktop's Linux engine. Each fixture was copied three times with a streaming tar parser and no compression:

| Fixture | Immediate children | Subtree content | Archive bytes | Median |
|---|---:|---:|---:|---:|
| Empty directory | 0 | 0 B | 1,536 B | 0.265 s |
| 10 one-byte files | 10 | 10 B | 11,776 B | 0.262 s |
| 1,000 one-byte files | 1,000 | 1,000 B | 1,025,536 B | 0.738 s |
| 10 files of 1 MiB | 10 | 10 MiB | 10,492,416 B | 0.288 s |
| 10 files of 16 MiB | 10 | 160 MiB | 167,778,816 B | 1.145 s |
| 10 files of 100 MiB | 10 | 1,000 MiB | 1,048,582,656 B | 7.517 s |
| One 160 MiB nested subtree plus one-byte sibling | 2 | 160 MiB + 1 B | 167,775,744 B | 1.367 s |

Directory headers arrived depth-first and lexically in this measurement, but ordering is not treated as an API guarantee. An early reader stop omitted immediate children and did not establish prompt daemon cancellation or read-ahead bounds; killing the CLI saved host processing but made the next copy of the same container substantially slower in some runs. Symlinks arrived as typeflag `2` with link targets and no target contents. The Engine API has the same recursive tar response; switching from CLI to HTTP does not add a listing primitive.

The decision is therefore to keep `FILES_LIST` withheld. Named output paths remain cheap and bounded, while a kind that needs discovery must use a backend that explicitly declares directory listing.

## Egress topology and limits

Closed mode is `docker run --network none`, enforced by the kernel namespace and portable across Docker Desktop, native Engine and compatible VM-backed engines. Allowlist mode creates an internal workload network, places a filtering CONNECT proxy on it, connects the proxy's second interface to an outbound network, and points the workload's HTTP proxy variables at the proxy. The workload has no direct route out; environment variables are advisory, while topology is the enforcement. The proxy does not decrypt TLS and resolves target names on the egress side, refusing private/link-local destinations as configured by the existing proxy source.

Host firewall and `DOCKER-USER`/iptables rules are not portable: rootless engines use another namespace and Docker Desktop/Colima/OrbStack/Rancher Desktop place the daemon in a VM. A Docker backend must not declare `NETWORK` merely because it has an allowlist. Docker's embedded DNS resolver forwards lookups through the daemon, and behavior on `--internal` networks was identified as an engine-specific unknown; a real negative-control suite must include DNS, not only TCP.

Allowlist networks require the engine feature that provides an unaddressed internal bridge. If the daemon cannot enforce that topology, acquire must refuse rather than silently use an addressed bridge. Network and proxy state must be inspected on reuse because network creation keyed only by name can adopt a network with different effective settings.

## Conformance and CI

Offline tests replace the `_docker()` seam with a fake and assert every argv, timeout, cleanup path, bytes result and error. They cover protocol declarations, package dependency purity, experimental warning behavior, context/client selection, image pull behavior, acquire races, warm reuse, restart/adoption, label purge, network teardown, hardening flags, tar metadata, symlink and parent checks, byte caps, timeout/cancellation and stale daemon failures.

The live test suite is lightweight and model-free. It must run real containers for both closed and allowlisted egress, warm reuse, restart and adoption, label-based purge, named-file round trips, oversize refusal, symlink refusal, call scope and DNS/network negatives. It belongs in pull-request CI on `ubuntu-latest`, which already ships Docker; Windows hosted runners lack a supported Linux-container setup and macOS hosted runners lack a usable nested Linux VM. Full model-backed samples belong in post-release verification, not the pull-request gate.

Images should come from a registry that will not make shared runner pulls flaky. Docker Hub documents a 100-pull-per-six-hours unauthenticated limit per IPv4 or IPv6 /64; MCR is already used by this repository, and GHCR is a documented public-package fallback, but neither should be described as unlimited without an authoritative limit.

The first package rollout needs its own package metadata, strict typing/lint/test configuration, workspace lock entry, release-please component, tag glob, publish workflow lists, smoke-install entry and documentation rows. Two small samples should mirror the existing Bicep and CodeAct examples: one Docker Bicep validation and one Docker CodeAct execution. The package's live suite should run before model-backed samples are added.

## Prior art and rejected alternatives

`testcontainers-python` demonstrates environment-variable-first Docker context discovery; `python-on-whales` demonstrates the deliberate CLI-wrapper philosophy; `llm-sandbox` validates session-scoped containers for iterative agent work; `epicbox` demonstrates simpler per-call containers and explicit resource limits; `aiodocker` is the main async API alternative. None changes the protocol decision.

Rejected alternatives are Docker API clients with unwanted dependency or timeout/stream behavior, bind mounts that create host-path and cross-platform semantics, host-level firewall rules that fail on rootless/VM-backed engines, `FILES_LIST` through guest utilities, automatic promotion to `hardened_container`, and a single backend flag for Docker Sandboxes. Each either weakens portability, makes an unverifiable security claim or conflates two isolation rungs.

## Remaining limits

- Docker-compatible engines other than Docker Desktop and Docker Engine are not officially qualified; Podman escape-hatch behavior remains best-effort.
- Windows without WSL is deferred, and Windows containers are out of scope.
- The Docker daemon's DNS forwarding behavior on internal networks remains an identified measurement gap.
- A container-rung backend cannot claim micro-VM or hardened-container isolation without a separate verified backend and conformance bar.
- The named-file pull surface does not establish visibility or confinement for arbitrary image mounts, tmpfs, `/proc`, `/sys` or `/dev` paths.
- Timeout recovery discards the sandbox; per-command in-container termination and a general environment-variable channel are not part of the protocol.
- Full model-backed Docker samples and production operational cleanup require the package and CI rollout documented in the current backend guide.
