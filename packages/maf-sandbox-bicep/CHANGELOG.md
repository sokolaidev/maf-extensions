# Changelog

All notable changes to `maf-sandbox-bicep` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.8...maf-sandbox-bicep-v0.10.0) (2026-08-29)


### ⚠ BREAKING CHANGES

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681))

### Features

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681)) ([739481d](https://github.com/sokolaidev/maf-extensions/commit/739481df3b7903c7f0015fee81282027666bc1ba))


### Fixes

* admit maf-sandbox 0.27 in the dependents' range, and require 0.26 in the samples ([#748](https://github.com/sokolaidev/maf-extensions/issues/748)) ([f905461](https://github.com/sokolaidev/maf-extensions/commit/f9054614f061ffacf53abbbc174501f2d5be5a74))
* require maf-sandbox 0.27.0 in the dependents and 0.27 in the samples, and admit 0.28 ([#751](https://github.com/sokolaidev/maf-extensions/issues/751)) ([49d2a75](https://github.com/sokolaidev/maf-extensions/commit/49d2a758f60f3ffd503e5a94ea2b082c4d36ce9e))

## [0.9.8](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.7...maf-sandbox-bicep-v0.9.8) (2026-08-26)


### Fixes

* require maf-sandbox 0.25.0 in the dependents and 0.25 in the samples, and admit 0.26 ([#690](https://github.com/sokolaidev/maf-extensions/issues/690)) ([07f4a03](https://github.com/sokolaidev/maf-extensions/commit/07f4a0316acc74b0dc9a71f15dc4b9be943922bd))

## [0.9.7](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.6...maf-sandbox-bicep-v0.9.7) (2026-08-25)


### Fixes

* require maf-sandbox 0.24.0 in the dependents and 0.24 in the samples, and admit 0.25 ([#665](https://github.com/sokolaidev/maf-extensions/issues/665)) ([b410d73](https://github.com/sokolaidev/maf-extensions/commit/b410d73ac866f2abd19cf3e550f60f26920d5344))

## [0.9.6](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.5...maf-sandbox-bicep-v0.9.6) (2026-08-24)


### Documentation

* wslc 0.11.1 and bicep 0.9.5 never reached PyPI either, and where their code ships ([#654](https://github.com/sokolaidev/maf-extensions/issues/654)) ([e198594](https://github.com/sokolaidev/maf-extensions/commit/e19859405ed78bdc2c356d5d8f2ceb727e44441c))

## [0.9.5](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.4...maf-sandbox-bicep-v0.9.5) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox-bicep` 0.9.5 to install. The publish run failed before the upload, on a check that refused a state no publishing order can reach around: every published sibling still capped below the core this version requires, and the first package to move can only ever be alone there ([#653](https://github.com/sokolaidev/maf-extensions/pull/653)). The tag records the right commit and no artifact was ever built from it.
>
> The run cannot simply be repeated. Release tags here cannot be moved, anything under `scripts/` binds at the ref being published — so the tag carries the check that refused it, not the fix — and the `pypi` environment admits tag refs only, so a dispatch from a branch that does carry the fix cannot mint a publishing credential. This version number is spent rather than reused. **The code these entries describe ships in 0.9.6**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### Fixes

* require maf-sandbox 0.23.1 in the dependents and 0.23 in the samples, and admit 0.24 ([#652](https://github.com/sokolaidev/maf-extensions/issues/652)) ([f03d7f0](https://github.com/sokolaidev/maf-extensions/commit/f03d7f06d48a44079bc53d57337b06c5440870ae))


### Documentation

* the four versions tagged on 24 August never reached PyPI ([#646](https://github.com/sokolaidev/maf-extensions/issues/646)) ([2d35b50](https://github.com/sokolaidev/maf-extensions/commit/2d35b504c9b3e6f84943ef8fba4a9dd92a2c303c))

## [0.9.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.3...maf-sandbox-bicep-v0.9.4) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox-bicep` 0.9.4 to install. The publish run failed before the upload, on a repository test that read the tags of a shallow checkout ([#645](https://github.com/sokolaidev/maf-extensions/pull/645)); the tag records the right commit and no artifact was ever built from it.
>
> Release tags here cannot be moved, so this version number is spent rather than reused. **The code these entries describe ships in 0.9.6**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### Fixes

* require maf-sandbox 0.22.0 in the dependents and 0.22 in the samples, and admit 0.23 ([#619](https://github.com/sokolaidev/maf-extensions/issues/619)) ([d8e122a](https://github.com/sokolaidev/maf-extensions/commit/d8e122a8f67e710704a4ffa0c11fdbebdaefb84e))

## [0.9.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.2...maf-sandbox-bicep-v0.9.3) (2026-08-23)


### Fixes

* require maf-sandbox 0.21.0 in the dependents and 0.21 in the samples, and admit 0.22 ([#596](https://github.com/sokolaidev/maf-extensions/issues/596)) ([1028a57](https://github.com/sokolaidev/maf-extensions/commit/1028a57e16d2fe5cb3aa0b3b948680e52fce90c3))

## [0.9.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.1...maf-sandbox-bicep-v0.9.2) (2026-08-22)


### Fixes

* require maf-sandbox 0.20.0 in the dependents and 0.20 in the samples, and admit 0.21 ([#564](https://github.com/sokolaidev/maf-extensions/issues/564)) ([727af26](https://github.com/sokolaidev/maf-extensions/commit/727af26c6db27a0de11a901d531f7183fda8426d))

## [0.9.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.9.0...maf-sandbox-bicep-v0.9.1) (2026-08-22)


### Documentation

* say what the 0.19 egress change breaks, and what to do about it ([#543](https://github.com/sokolaidev/maf-extensions/issues/543)) ([4b736de](https://github.com/sokolaidev/maf-extensions/commit/4b736de3368feb7fca0ffe36f2b607214d376d44))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.8.1...maf-sandbox-bicep-v0.9.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))


### Fixes

* require maf-sandbox 0.19.0 in the dependents and 0.19 in the samples, and admit 0.20 ([#540](https://github.com/sokolaidev/maf-extensions/issues/540)) ([ae825c2](https://github.com/sokolaidev/maf-extensions/commit/ae825c2c8fd5e105402470c788b24371a77efa7c))

## [0.8.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.8.0...maf-sandbox-bicep-v0.8.1) (2026-08-21)


### Fixes

* **kinds:** wire kinds to framework-owned call directories ([#500](https://github.com/sokolaidev/maf-extensions/issues/500)) ([91fd4d1](https://github.com/sokolaidev/maf-extensions/commit/91fd4d18b3ef9dff5223713d26e78614eb847fb7))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.5...maf-sandbox-bicep-v0.8.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))


### Fixes

* require maf-sandbox 0.18.0 in the dependents and 0.18 in the samples, and admit 0.19 ([#494](https://github.com/sokolaidev/maf-extensions/issues/494)) ([dd12d77](https://github.com/sokolaidev/maf-extensions/commit/dd12d7745b526052268b2124803e549b1e8c3d7f))

## [0.7.5](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.4...maf-sandbox-bicep-v0.7.5) (2026-08-19)


### Fixes

* require maf-sandbox 0.17.0 in the dependents and 0.17 in the samples, and admit 0.18 ([#472](https://github.com/sokolaidev/maf-extensions/issues/472)) ([dffd936](https://github.com/sokolaidev/maf-extensions/commit/dffd936ed3cb3c6a49d1dce0776ba321ee4d1dda))

## [0.7.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.3...maf-sandbox-bicep-v0.7.4) (2026-08-17)


### Bug Fixes

* require maf-sandbox 0.16.0 in the dependents and 0.16 in the samples, and admit 0.17 ([#386](https://github.com/sokolaidev/maf-extensions/issues/386)) ([7133401](https://github.com/sokolaidev/maf-extensions/commit/713340192dbc710c9c18f498a6615fc401332682))

## [0.7.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.2...maf-sandbox-bicep-v0.7.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.7.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.1...maf-sandbox-bicep-v0.7.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.7.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.7.0...maf-sandbox-bicep-v0.7.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-bicep-v0.6.0...maf-sandbox-bicep-v0.7.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

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
