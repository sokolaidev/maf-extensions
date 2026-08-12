# Changelog

All notable changes to `maf-sandbox` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.9.0...maf-sandbox-v0.10.0) (2026-08-12)


### Features

* name a symlink in the protocol, and hold every FILES_OUT backend to one conformance suite ([#215](https://github.com/sokolaidev/maf-extensions/issues/215)) ([051c4c2](https://github.com/sokolaidev/maf-extensions/commit/051c4c2a906dcca4f5bd45654834061befc71308))


### Documentation

* give the FILES_OUT pull surface a section an app author can start from ([#230](https://github.com/sokolaidev/maf-extensions/issues/230)) ([b41dd80](https://github.com/sokolaidev/maf-extensions/commit/b41dd8026b7ebb7f471f508df33877dce3c028d8))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.8.0...maf-sandbox-v0.9.0) (2026-08-12)


### Features

* a shared maf_sandbox.paths for guest-path confinement and the ancestor walk ([#216](https://github.com/sokolaidev/maf-extensions/issues/216)) ([9e99c2a](https://github.com/sokolaidev/maf-extensions/commit/9e99c2aa18b83041af4166903157b73975cee195))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.7.0...maf-sandbox-v0.8.0) (2026-08-11)


### Features

* the host-tools safety contract lands before anything can dispatch ([#133](https://github.com/sokolaidev/maf-extensions/issues/133)) ([#155](https://github.com/sokolaidev/maf-extensions/issues/155)) ([159a2a5](https://github.com/sokolaidev/maf-extensions/commit/159a2a5dfb3b454450ce634858721afa6c8db105))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.6.1...maf-sandbox-v0.7.0) (2026-08-11)


### Features

* let a workload name its artifacts at call time, and land them under a name of their own ([#156](https://github.com/sokolaidev/maf-extensions/issues/156)) ([53edc4d](https://github.com/sokolaidev/maf-extensions/commit/53edc4db92b4c788864f6b9781514b60d8ae2a00))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.6.0...maf-sandbox-v0.6.1) (2026-08-11)


### Documentation

* keep the sandbox protocol platform-neutral so the guest-platform axis lands additively ([#145](https://github.com/sokolaidev/maf-extensions/issues/145)) ([31906e8](https://github.com/sokolaidev/maf-extensions/commit/31906e8e9ea5e5044dc800acdd97a58b84cf179f))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.5.0...maf-sandbox-v0.6.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113))

### Features

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113)) ([92e7a0f](https://github.com/sokolaidev/maf-extensions/commit/92e7a0f22cc4c3f757218d5ec2ae58a41d912f1f))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.4.0...maf-sandbox-v0.5.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96))

### Features

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96)) ([b5990ee](https://github.com/sokolaidev/maf-extensions/commit/b5990ee492bca09a0e267172216c087be1db647a))


### Documentation

* tell 0.4.x users what to change for the isolation floor ([#101](https://github.com/sokolaidev/maf-extensions/issues/101)) ([5d57d01](https://github.com/sokolaidev/maf-extensions/commit/5d57d01c656883181d5fbccc21532cd85f3b4c4d))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.3.0...maf-sandbox-v0.4.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87))

### Bug Fixes

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87)) ([fa321cf](https://github.com/sokolaidev/maf-extensions/commit/fa321cf53f643f9e30df910fc8e46c6a938d6605))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.2...maf-sandbox-v0.3.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73))

### Features

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73)) ([c1f3395](https://github.com/sokolaidev/maf-extensions/commit/c1f3395e6be0c050e65365bdfdf1a65a22bac442))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.1...maf-sandbox-v0.2.2) (2026-08-09)


### Bug Fixes

* **aca:** stop two concurrent calls for one key from creating two sandboxes ([#58](https://github.com/sokolaidev/maf-extensions/issues/58)) ([b3641ea](https://github.com/sokolaidev/maf-extensions/commit/b3641ea299e9805039cdbad3b3ab4745a6d40f39))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.0...maf-sandbox-v0.2.1) (2026-08-08)


### Documentation

* stop naming the current version in the experimental notices ([#52](https://github.com/sokolaidev/maf-extensions/issues/52)) ([a358066](https://github.com/sokolaidev/maf-extensions/commit/a35806659b5da63b144e111e2d5e8023f0e00adc))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.1.1...maf-sandbox-v0.2.0) (2026-08-08)


### ⚠ BREAKING CHANGES

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40))

### Features

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40)) ([4310250](https://github.com/sokolaidev/maf-extensions/commit/43102501bae173710fedddbb1ea7ab5a27e2def4))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.1.0...maf-sandbox-v0.1.1) (2026-08-08)


### Documentation

* add a samples/ tree, starting with a one-turn Bicep validation agent ([#19](https://github.com/sokolaidev/maf-extensions/issues/19)) ([c2fa24d](https://github.com/sokolaidev/maf-extensions/commit/c2fa24d55b2e35b80ae027d6959067c6ebec1224))

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox` was split out of a production agent application's sandbox-routing module as the protocol seam between a host application and any sandbox provider: `Sandbox`, `SandboxBackend`, `SandboxKey`, `SandboxSpec`, `WorkspaceContext`, `SandboxRouter`, `SandboxPurger`, and the deployed-isolation rule (`DEPLOYED_ISOLATION`, `SandboxBackendNotPermitted`). This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the protocol itself.
