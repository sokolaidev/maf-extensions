# maf-sandbox-mxc

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-mxc)](https://pypi.org/project/maf-sandbox-mxc/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-mxc)](https://pypi.org/project/maf-sandbox-mxc/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** The API may change without notice. Importing the package emits `MafSandboxMxcExperimentalWarning`.

Durable MXC Python sessions, bounded helper transports and mutual-TLS coordination. This package is unreleased and experimental. It provides the host runtime previously kept under `scripts/experiments`; it does not yet implement the suite's `SandboxBackend` protocol.

## Runtime surfaces

- `shared_store`, `native_journal` and `shared_call` provide SQLite publication, checkpoint recovery, result replay, quotas, freshness and cleanup ownership.
- `remote_authority`, `remote_api`, `remote_host` and `remote_control` provide fenced ownership and mutually authenticated checkpoint transfer between configured hosts.
- `files` and `streams` provide bounded transport for the separately qualified custom-kernel profiles.
- `host_call` supervises the pinned native helper with bounded output. The legacy single-owner CLI and crash probes remain in repository tooling.

The package uses the Python standard library. Native helper binaries, kernels and rootfs images are provisioned separately, with exact identities supplied by the host. Installation does not download, compile or launch them. Existing stores retain their original helper/profile bindings; packaging does not migrate checkpoints or change wire/store formats.

## Development and qualification

Install from this directory with `pip install .`, or use the repository's uv workspace. Build and qualification tooling remains under `scripts/experiments`, importing this package. Generated reports belong outside Git and hosted reports remain workflow artifacts subject to retention.

MXC 1.1 rich-state qualification is recorded separately from the 1.0.1 custom file/stream profiles. A package build or offline test pass does not establish native isolation, cross-platform checkpoint portability, physical power-loss recovery or production readiness. Networking remains closed for the qualified Hyperlight profile.

The [MXC research record](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/mxc-backend.md) contains the accepted design, implementation status, native evidence, reproduction instructions and workflow entry points.
