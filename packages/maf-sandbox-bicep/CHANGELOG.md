# Changelog

All notable changes to `maf-sandbox-bicep` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.6...maf-sandbox-bicep-v0.6.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* "workspace" is gone from the public vocabulary; rename at the call site. `WorkspaceContext` is now `CallerContext`, and `make_workspace_context` is `make_caller_context` — whose first parameter is `list_files` rather than `store_walker`. `bicep_validate_tool` and `execute_code_tool` take `file_store=` where they took `workspace_store=`. `maf_sandbox_bicep.safe_workspace_path` is `safe_listed_path`. `work_dir` and `working_directory` are unchanged: they name the guest's working directory and were never this concept.

### Features

* retire "workspace" from the vocabulary — CallerContext, file_store, list_files ([#240](https://github.com/sokolaidev/maf-extensions/issues/240)) ([e746982](https://github.com/sokolaidev/maf-extensions/commit/e746982d42707e6c4599ba1ec927797b25d360e8))


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.5.6](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.5...maf-sandbox-bicep-v0.5.6) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.5.5](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.4...maf-sandbox-bicep-v0.5.5) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.5.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.3...maf-sandbox-bicep-v0.5.4) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.5.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.2...maf-sandbox-bicep-v0.5.3) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.5.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.1...maf-sandbox-bicep-v0.5.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.5.0...maf-sandbox-bicep-v0.5.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.6.0 in the dependents' range ([#150](https://github.com/sokolaidev/maf-extensions/issues/150)) ([f2e2ca1](https://github.com/sokolaidev/maf-extensions/commit/f2e2ca13d447f5885dc6d2b2d0b8d2f3e5bbb206))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.4.0...maf-sandbox-bicep-v0.5.0) (2026-08-10)

> **Correction, added after the release.** This version contains **no functional change to `maf-sandbox-bicep`**. Its entire diff against 0.4.0 is a one-word comment: a parameter named in a code comment was renamed to match its new spelling. There is no breaking change and no feature here, and upgrading from 0.4.0 requires nothing.
>
> The entries below are accurate about the commit and misleading about this package. [#113](https://github.com/sokolaidev/maf-extensions/pull/113) implemented the `FILES_OUT` surface in **`maf-sandbox`**, and that is where its breaking change and its feature live — released as `maf-sandbox` 0.6.0. The comment fix rode along in the same commit, and release-please attributes a commit to every package whose files it touches, so this package inherited the commit's type and its changelog text.
>
> Left in place rather than deleted: the generated entries are the honest record of what release-please saw, and removing them would hide why this version exists at all.

### ⚠ BREAKING CHANGES

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113))

### Features

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113)) ([92e7a0f](https://github.com/sokolaidev/maf-extensions/commit/92e7a0f22cc4c3f757218d5ec2ae58a41d912f1f))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.3.1...maf-sandbox-bicep-v0.4.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96))

### Features

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96)) ([b5990ee](https://github.com/sokolaidev/maf-extensions/commit/b5990ee492bca09a0e267172216c087be1db647a))


### Bug Fixes

* admit maf-sandbox 0.4.0 in the dependents' range ([#92](https://github.com/sokolaidev/maf-extensions/issues/92)) ([101dccb](https://github.com/sokolaidev/maf-extensions/commit/101dccbcf4178d7155d646361d1ea3422cac6f7f))
* require maf-sandbox 0.5.0 in the packages that use it ([#102](https://github.com/sokolaidev/maf-extensions/issues/102)) ([cd19b00](https://github.com/sokolaidev/maf-extensions/commit/cd19b0051254e32683dfa07580506e44fb71f41a))

## [0.3.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.3.0...maf-sandbox-bicep-v0.3.1) (2026-08-09)


### Bug Fixes

* admit maf-sandbox 0.3.0 in the dependents' range ([#78](https://github.com/sokolaidev/maf-extensions/issues/78)) ([89ccab0](https://github.com/sokolaidev/maf-extensions/commit/89ccab01cb4485f8d13ed1b75ae46b074f2afff2))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.2.2...maf-sandbox-bicep-v0.3.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73))

### Features

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73)) ([c1f3395](https://github.com/sokolaidev/maf-extensions/commit/c1f3395e6be0c050e65365bdfdf1a65a22bac442))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.2.1...maf-sandbox-bicep-v0.2.2) (2026-08-09)


### Bug Fixes

* **aca:** stop two concurrent calls for one key from creating two sandboxes ([#58](https://github.com/sokolaidev/maf-extensions/issues/58)) ([b3641ea](https://github.com/sokolaidev/maf-extensions/commit/b3641ea299e9805039cdbad3b3ab4745a6d40f39))

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
