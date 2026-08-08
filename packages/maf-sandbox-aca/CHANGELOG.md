# Changelog

All notable changes to `maf-sandbox-aca` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [Unreleased]

### Added

- The Quickstart now links a runnable end-to-end sample, [`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) — a one-turn agent that builds this backend from environment variables, puts it behind a `deployed=True` router, validates a Bicep file, and disposes the sandbox afterwards rather than leaving it to the auto-delete timer.

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox-aca` was split out of a production agent application's Azure Container Apps Sandboxes module as the backend implementing `maf_sandbox.SandboxBackend`: VM isolation, Deny-default egress with a per-spec allowlist, no ambient identity inside the sandbox, and label-based multi-replica-safe purge. `azure-containerapps-sandbox` becomes a hard dependency in this release — the previous `[aca]` extra is retired, and a host's optionality now lives at its own `bicep-sandbox` extra instead. This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the backend itself.
