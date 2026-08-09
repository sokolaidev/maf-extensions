# Changelog

All notable changes to `maf-sandbox-acas` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

Releases up to and including `0.2.3` were published as **`maf-sandbox-aca`**, and the entries below name it as it was — their tags and compare links point at real history and are left as they were written. `maf-sandbox-aca` is not maintained past `0.2.3`; PyPI names cannot be reused, so the rename is a new distribution rather than a continuation of that one.

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.2.3...maf-sandbox-acas-v0.3.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73))

### Features

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73)) ([c1f3395](https://github.com/sokolaidev/maf-extensions/commit/c1f3395e6be0c050e65365bdfdf1a65a22bac442))

## [0.2.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-aca-v0.2.2...maf-sandbox-aca-v0.2.3) (2026-08-09)


### Documentation

* **aca:** call the ACA sandbox a sandbox, not a VM ([#71](https://github.com/sokolaidev/maf-extensions/issues/71)) ([5ca6adf](https://github.com/sokolaidev/maf-extensions/commit/5ca6adfe4882b0cc1422fbda95c5a1948dd46449))
* **aca:** point this package at maf-sandbox-acas ([#74](https://github.com/sokolaidev/maf-extensions/issues/74)) ([c785246](https://github.com/sokolaidev/maf-extensions/commit/c78524621c5049a18a14376fb15430a746b0f880))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-aca-v0.2.1...maf-sandbox-aca-v0.2.2) (2026-08-09)


### Bug Fixes

* **aca:** stop two concurrent calls for one key from creating two sandboxes ([#58](https://github.com/sokolaidev/maf-extensions/issues/58)) ([b3641ea](https://github.com/sokolaidev/maf-extensions/commit/b3641ea299e9805039cdbad3b3ab4745a6d40f39))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-aca-v0.2.0...maf-sandbox-aca-v0.2.1) (2026-08-08)


### Documentation

* stop naming the current version in the experimental notices ([#52](https://github.com/sokolaidev/maf-extensions/issues/52)) ([a358066](https://github.com/sokolaidev/maf-extensions/commit/a35806659b5da63b144e111e2d5e8023f0e00adc))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-aca-v0.1.1...maf-sandbox-aca-v0.2.0) (2026-08-08)


### ⚠ BREAKING CHANGES

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40))

### Features

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40)) ([4310250](https://github.com/sokolaidev/maf-extensions/commit/43102501bae173710fedddbb1ea7ab5a27e2def4))


### Bug Fixes

* admit the maf-sandbox version being released, so the release can build ([#46](https://github.com/sokolaidev/maf-extensions/issues/46)) ([bee6930](https://github.com/sokolaidev/maf-extensions/commit/bee69302aadb1608f9e640c891edc43ce7f94531))
* require the maf-sandbox that has the API these packages use ([#47](https://github.com/sokolaidev/maf-extensions/issues/47)) ([63f6677](https://github.com/sokolaidev/maf-extensions/commit/63f667795b23033748f88d75bd903462c65a383d))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-aca-v0.1.0...maf-sandbox-aca-v0.1.1) (2026-08-08)


### Documentation

* add a samples/ tree, starting with a one-turn Bicep validation agent ([#19](https://github.com/sokolaidev/maf-extensions/issues/19)) ([c2fa24d](https://github.com/sokolaidev/maf-extensions/commit/c2fa24d55b2e35b80ae027d6959067c6ebec1224))

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox-aca` was split out of a production agent application's Azure Container Apps Sandboxes module as the backend implementing `maf_sandbox.SandboxBackend`: VM isolation, Deny-default egress with a per-spec allowlist, no ambient identity inside the sandbox, and label-based multi-replica-safe purge. `azure-containerapps-sandbox` becomes a hard dependency in this release — the previous `[aca]` extra is retired, and a host's optionality now lives at its own `bicep-sandbox` extra instead. This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the backend itself.
