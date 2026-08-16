# Changelog

All notable changes to `maf-sandbox` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.15.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.14.0...maf-sandbox-v0.15.0) (2026-08-16)


### Features

* a host-tool transport an EXEC backend can implement, over request and response files ([#327](https://github.com/sokolaidev/maf-extensions/issues/327)) ([680abb0](https://github.com/sokolaidev/maf-extensions/commit/680abb0855a2396723c084c071bc2e2c7a164acb))


### Documentation

* the ladder one-liner still named the renamed bottom rung ([#348](https://github.com/sokolaidev/maf-extensions/issues/348)) ([76a175c](https://github.com/sokolaidev/maf-extensions/commit/76a175c8126c5585b4746cb4419ce686eb7e05fb))

## [0.14.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.13.0...maf-sandbox-v0.14.0) (2026-08-14)


### ⚠ BREAKING CHANGES

* rename Isolation.PROCESS to NONE, and reserve the old spelling ([#331](https://github.com/sokolaidev/maf-extensions/issues/331))

### Features

* rename Isolation.PROCESS to NONE, and reserve the old spelling ([#331](https://github.com/sokolaidev/maf-extensions/issues/331)) ([647e7a2](https://github.com/sokolaidev/maf-extensions/commit/647e7a2bd3d72abf0c2cf13ed2c8172dccfdfc32))

## [0.13.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.12.0...maf-sandbox-v0.13.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* refuse a conformance subject that declares no FILES_OUT, and hold every backend that serves it to the suite ([#298](https://github.com/sokolaidev/maf-extensions/issues/298)) ([ce855a5](https://github.com/sokolaidev/maf-extensions/commit/ce855a525ec0a5ba0bd8a0e231b3f93d3c99c1b2))

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.11.0...maf-sandbox-v0.12.0) (2026-08-12)


### Features

* package the three app-facing helpers every sample wrote for itself ([#239](https://github.com/sokolaidev/maf-extensions/issues/239)) ([a0e77c0](https://github.com/sokolaidev/maf-extensions/commit/a0e77c0924d36bd8515eeb939f6b2a3e70e2ec4d))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.10.0...maf-sandbox-v0.11.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* "workspace" is gone from the public vocabulary; rename at the call site. `WorkspaceContext` is now `CallerContext`, and `make_workspace_context` is `make_caller_context` — whose first parameter is `list_files` rather than `store_walker`. `bicep_validate_tool` and `execute_code_tool` take `file_store=` where they took `workspace_store=`. `maf_sandbox_bicep.safe_workspace_path` is `safe_listed_path`. `work_dir` and `working_directory` are unchanged: they name the guest's working directory and were never this concept.

### Features

* retire "workspace" from the vocabulary — CallerContext, file_store, list_files ([#240](https://github.com/sokolaidev/maf-extensions/issues/240)) ([e746982](https://github.com/sokolaidev/maf-extensions/commit/e746982d42707e6c4599ba1ec927797b25d360e8))

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
