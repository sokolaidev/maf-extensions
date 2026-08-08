# Changelog

All notable changes to `maf-sandbox-bicep` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.2.0...maf-sandbox-bicep-v0.2.1) (2026-08-08)


### Documentation

* stop naming the current version in the experimental notices ([#52](https://github.com/sokolaidev/maf-extensions/issues/52)) ([a358066](https://github.com/sokolaidev/maf-extensions/commit/a35806659b5da63b144e111e2d5e8023f0e00adc))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.1.2...maf-sandbox-bicep-v0.2.0) (2026-08-08)


### ⚠ BREAKING CHANGES

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40))

### Features

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40)) ([4310250](https://github.com/sokolaidev/maf-extensions/commit/43102501bae173710fedddbb1ea7ab5a27e2def4))


### Bug Fixes

* admit the maf-sandbox version being released, so the release can build ([#46](https://github.com/sokolaidev/maf-extensions/issues/46)) ([bee6930](https://github.com/sokolaidev/maf-extensions/commit/bee69302aadb1608f9e640c891edc43ce7f94531))
* require the maf-sandbox that has the API these packages use ([#47](https://github.com/sokolaidev/maf-extensions/issues/47)) ([63f6677](https://github.com/sokolaidev/maf-extensions/commit/63f667795b23033748f88d75bd903462c65a383d))

## [0.1.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.1.1...maf-sandbox-bicep-v0.1.2) (2026-08-08)


### Bug Fixes

* tell a listing miss apart from an unsafe name ([#27](https://github.com/sokolaidev/maf-extensions/issues/27)) ([a1f8ead](https://github.com/sokolaidev/maf-extensions/commit/a1f8ead17de82d3952c0bc92f7a55bb2e8c7d0ea))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.1.0...maf-sandbox-bicep-v0.1.1) (2026-08-08)


### Documentation

* add a samples/ tree, starting with a one-turn Bicep validation agent ([#19](https://github.com/sokolaidev/maf-extensions/issues/19)) ([c2fa24d](https://github.com/sokolaidev/maf-extensions/commit/c2fa24d55b2e35b80ae027d6959067c6ebec1224))

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox-bicep` was split out of `maf-sandbox-aca` as the first sandbox *kind*: `bicep_validate`, a Microsoft Agent Framework tool that writes an agent's authored files into a sandbox, runs `bicep build` and `bicep lint` there, and returns the compiler's SARIF diagnostics as structured text. It imports no Azure SDK and no sandbox lifecycle code — only `maf-sandbox`'s protocol and `agent-framework-core`. This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the tool itself.
