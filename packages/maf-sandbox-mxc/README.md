# maf-sandbox-mxc

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-mxc)](https://pypi.org/project/maf-sandbox-mxc/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-mxc)](https://pypi.org/project/maf-sandbox-mxc/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** The API may change without notice. Importing the package emits `MafSandboxMxcExperimentalWarning`.

Durable MXC Python sessions, bounded helper transports and mutual-TLS coordination. This package is unreleased and experimental. `MxcSandboxBackend` supplies a closed-network native Python program channel with bounded input and output files. Native adapter qualification is tracked separately from the previously qualified helper profiles.

## Runtime surfaces

- `shared_store`, `native_journal` and `shared_call` provide SQLite publication, checkpoint recovery, result replay, quotas, freshness and cleanup ownership.
- `remote_authority`, `remote_api`, `remote_host` and `remote_control` provide fenced ownership and mutually authenticated checkpoint transfer between configured hosts.
- `files` and `streams` provide bounded transport for the separately qualified custom-kernel profiles.
- `host_call` supervises the pinned native helper with bounded output. The legacy single-owner CLI and crash probes remain in repository tooling.

The adapter depends on `maf-sandbox`; the underlying journal and transports use the Python standard library. Native helper binaries, kernels and rootfs images are provisioned separately, with exact identities supplied by the host. Installation does not download, compile or launch them. Existing stores retain their original helper/profile bindings; packaging does not migrate checkpoints or change wire/store formats.

## Native adapter

Construct `MxcSandboxConfig` with the host-owned helper, startup directory, private journal root, a runtime profile name, expected helper SHA-256, `startup_identity(startup)`, `Limits` and `ScratchLimits`. Pin identities from qualification evidence rather than accepting assets from an untrusted request. Keep the configured assets immutable while the backend is open. Call `await backend.initialize()` once for a new root; subsequent backend instances reopen that journal on acquisition and refuse a missing or mismatched store. `await backend.aclose()` releases ownership while retaining checkpoints and completed results. Disposal retires the selected session; scope purge also includes sessions not acquired by this process.

The first channel uses the retained MXC 1.0.1 file-capable custom profile with the directory-collection helper extension. The MXC 1.1 console profile is a separate qualification and cannot supply this file/independent-stream contract. Allowlisted and unrestricted network requests, guest images, shell execution and host tools are refused. Use `ProgramRequirements(max_program_bytes=65536)` and a call timeout of at most 90 seconds. Configure CodeAct file transfer limits within the default 16 MiB / 64-file envelope. Output exceeding either independent one-MiB stream allowance refuses publication rather than returning an apparently complete `ExecResult`.

Fresh interpreter state is the default. Set `persistent=True` and explicitly select router `Cleanup.RECLAIM` to retain Python state across successful calls; the router's normal disposal policy remains appropriate for fresh calls. Every successful native call collects its bounded directory, reclaims native call files and commits the checkpoint with its result before acknowledgment. Collected files are immutable host-side snapshots for delivery; they are not paths into the host filesystem. The backend rejects concurrent direct access outside its `call_admission` scope and waits for native shutdown on cancellation.

Direct `run_call` users supply a stable call ID and the exact original request when retrying after recovery. An interrupted or expired identity is refused rather than executed again. CodeAct allocates a new call directory for each new tool call; issuing a new tool call does not automatically replay an earlier one. The existing coordinator and freshness-witness APIs remain separate from this local adapter. Whole-machine recovery, allowlist enforcement and production qualification remain outside this candidate.

## Development and qualification

Install from this directory with `pip install .`, or use the repository's uv workspace. Build and qualification tooling remains under `scripts/experiments`, importing this package. Generated reports belong outside Git and hosted reports remain workflow artifacts subject to retention.

MXC 1.1 rich-state qualification is recorded separately from the 1.0.1 custom file/stream profiles. A package build or offline test pass does not establish native isolation, cross-platform checkpoint portability, physical power-loss recovery or production readiness. Networking remains closed for the qualified Hyperlight profile.

The [MXC research record](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/mxc-backend.md) contains the accepted design, implementation status, native evidence, reproduction instructions and workflow entry points.
