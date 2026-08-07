# Changelog

All notable changes to `maf-sandbox` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.1.0] - 2026-08-07

Initial extraction (issue #697). `maf-sandbox` was split out of the host application's `src/sandbox-router` (issue #695, renamed per #696) as the protocol seam between a host application and any sandbox provider: `Sandbox`, `SandboxBackend`, `SandboxKey`, `SandboxSpec`, `WorkspaceContext`, `SandboxRouter`, `SandboxPurger`, and the deployed-isolation rule (`DEPLOYED_ISOLATION`, `SandboxBackendNotPermitted`). This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the protocol itself.
