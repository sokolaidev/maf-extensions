# Changelog

## [0.4.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.2...maf-sandbox-codeact-v0.4.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.4.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.1...maf-sandbox-codeact-v0.4.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.4.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.0...maf-sandbox-codeact-v0.4.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.3.0...maf-sandbox-codeact-v0.4.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.3...maf-sandbox-codeact-v0.3.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* "workspace" is gone from the public vocabulary; rename at the call site. `WorkspaceContext` is now `CallerContext`, and `make_workspace_context` is `make_caller_context` — whose first parameter is `list_files` rather than `store_walker`. `bicep_validate_tool` and `execute_code_tool` take `file_store=` where they took `workspace_store=`. `maf_sandbox_bicep.safe_workspace_path` is `safe_listed_path`. `work_dir` and `working_directory` are unchanged: they name the guest's working directory and were never this concept.

### Features

* retire "workspace" from the vocabulary — CallerContext, file_store, list_files ([#240](https://github.com/sokolaidev/maf-extensions/issues/240)) ([e746982](https://github.com/sokolaidev/maf-extensions/commit/e746982d42707e6c4599ba1ec927797b25d360e8))


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.2.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.2...maf-sandbox-codeact-v0.2.3) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.1...maf-sandbox-codeact-v0.2.2) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.0...maf-sandbox-codeact-v0.2.1) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.3...maf-sandbox-codeact-v0.2.0) (2026-08-11)


### Features

* a workspace channel for CodeAct — files in, and outputs a portable backend can serve ([#158](https://github.com/sokolaidev/maf-extensions/issues/158)) ([322c579](https://github.com/sokolaidev/maf-extensions/commit/322c579fc8fa2a951af6777ada900d09cf30252f))

## [0.1.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.2...maf-sandbox-codeact-v0.1.3) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.1.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.1...maf-sandbox-codeact-v0.1.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.0...maf-sandbox-codeact-v0.1.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.6.0 in the dependents' range ([#150](https://github.com/sokolaidev/maf-extensions/issues/150)) ([f2e2ca1](https://github.com/sokolaidev/maf-extensions/commit/f2e2ca13d447f5885dc6d2b2d0b8d2f3e5bbb206))

## [0.1.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.0.1...maf-sandbox-codeact-v0.1.0) (2026-08-10)


### Features

* maf-sandbox-codeact — run agent-written Python in a sandbox, on any backend ([#97](https://github.com/sokolaidev/maf-extensions/issues/97)) ([f13b051](https://github.com/sokolaidev/maf-extensions/commit/f13b0515699e1239623a0969ed811d74483b2fbe))

## Changelog

All notable changes to `maf-sandbox-codeact` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.
