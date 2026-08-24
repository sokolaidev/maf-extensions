# Changelog

## [0.11.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.11.0...maf-sandbox-wslc-v0.11.1) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox-wslc` 0.11.1 to install. The publish run failed before the upload, on a check that refused a state no publishing order can reach around: every published sibling still capped below the core this version requires, and the first package to move can only ever be alone there ([#653](https://github.com/sokolaidev/maf-extensions/pull/653)). The tag records the right commit and no artifact was ever built from it.
>
> The run cannot simply be repeated. Release tags here cannot be moved, anything under `scripts/` binds at the ref being published — so the tag carries the check that refused it, not the fix — and the `pypi` environment admits tag refs only, so a dispatch from a branch that does carry the fix cannot mint a publishing credential. This version number is spent rather than reused. **The code these entries describe ships in 0.11.2**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### Fixes

* require maf-sandbox 0.23.1 in the dependents and 0.23 in the samples, and admit 0.24 ([#652](https://github.com/sokolaidev/maf-extensions/issues/652)) ([f03d7f0](https://github.com/sokolaidev/maf-extensions/commit/f03d7f06d48a44079bc53d57337b06c5440870ae))


### Documentation

* the four versions tagged on 24 August never reached PyPI ([#646](https://github.com/sokolaidev/maf-extensions/issues/646)) ([2d35b50](https://github.com/sokolaidev/maf-extensions/commit/2d35b504c9b3e6f84943ef8fba4a9dd92a2c303c))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.10.2...maf-sandbox-wslc-v0.11.0) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox-wslc` 0.11.0 to install. The publish run failed before the upload, on a repository test that read the tags of a shallow checkout ([#645](https://github.com/sokolaidev/maf-extensions/pull/645)); the tag records the right commit and no artifact was ever built from it.
>
> Release tags here cannot be moved, so this version number is spent rather than reused. **The code these entries describe ships in 0.11.2**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### ⚠ BREAKING CHANGES

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609))

### Features

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609)) ([6fcfcf6](https://github.com/sokolaidev/maf-extensions/commit/6fcfcf6259874bbcb4f02ac23bddcc92ae6d8550))


### Fixes

* require maf-sandbox 0.22.0 in the dependents and 0.22 in the samples, and admit 0.23 ([#619](https://github.com/sokolaidev/maf-extensions/issues/619)) ([d8e122a](https://github.com/sokolaidev/maf-extensions/commit/d8e122a8f67e710704a4ffa0c11fdbebdaefb84e))

## [0.10.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.10.1...maf-sandbox-wslc-v0.10.2) (2026-08-23)


### Fixes

* require maf-sandbox 0.21.0 in the dependents and 0.21 in the samples, and admit 0.22 ([#596](https://github.com/sokolaidev/maf-extensions/issues/596)) ([1028a57](https://github.com/sokolaidev/maf-extensions/commit/1028a57e16d2fe5cb3aa0b3b948680e52fce90c3))

## [0.10.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.10.0...maf-sandbox-wslc-v0.10.1) (2026-08-22)


### Fixes

* require maf-sandbox 0.20.0 in the dependents and 0.20 in the samples, and admit 0.21 ([#564](https://github.com/sokolaidev/maf-extensions/issues/564)) ([727af26](https://github.com/sokolaidev/maf-extensions/commit/727af26c6db27a0de11a901d531f7183fda8426d))

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.9.0...maf-sandbox-wslc-v0.10.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))
* **backends:** answer run_code on every shipped backend ([#531](https://github.com/sokolaidev/maf-extensions/issues/531)) ([7bf3cd2](https://github.com/sokolaidev/maf-extensions/commit/7bf3cd2048b7c6f41d2b1b14c79f52753f3c1db8))


### Fixes

* require maf-sandbox 0.19.0 in the dependents and 0.19 in the samples, and admit 0.20 ([#540](https://github.com/sokolaidev/maf-extensions/issues/540)) ([ae825c2](https://github.com/sokolaidev/maf-extensions/commit/ae825c2c8fd5e105402470c788b24371a77efa7c))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.8.0...maf-sandbox-wslc-v0.9.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))


### Fixes

* require maf-sandbox 0.18.0 in the dependents and 0.18 in the samples, and admit 0.19 ([#494](https://github.com/sokolaidev/maf-extensions/issues/494)) ([dd12d77](https://github.com/sokolaidev/maf-extensions/commit/dd12d7745b526052268b2124803e549b1e8c3d7f))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.7.0...maf-sandbox-wslc-v0.8.0) (2026-08-19)


### ⚠ BREAKING CHANGES

* **protocol:** `Sandbox` gains `remove(path, *, working_directory, recursive=False)`. An implementation that does not define it no longer satisfies the protocol. Backends that cannot confine a removal should raise `NotImplementedError` and not declare `Capability.FILES_DELETE`, as `maf-sandbox-wslc` does.

### Features

* **protocol:** a sandbox can be asked to delete what a workload put there ([#452](https://github.com/sokolaidev/maf-extensions/issues/452)) ([2453820](https://github.com/sokolaidev/maf-extensions/commit/245382036ba1e2ddc18dea79b8e97d2cfb561935))
* **sandbox:** probes for every capability a backend claims, and CI that enumerates backends rather than listing them ([#462](https://github.com/sokolaidev/maf-extensions/issues/462)) ([f0915c7](https://github.com/sokolaidev/maf-extensions/commit/f0915c71819c729cd33aa130749fffc8d69fa377))


### Fixes

* require maf-sandbox 0.17.0 in the dependents and 0.17 in the samples, and admit 0.18 ([#472](https://github.com/sokolaidev/maf-extensions/issues/472)) ([dffd936](https://github.com/sokolaidev/maf-extensions/commit/dffd936ed3cb3c6a49d1dce0776ba321ee4d1dda))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.6.3...maf-sandbox-wslc-v0.7.0) (2026-08-17)


### Features

* **backends:** each backend exports the name `selected=` matches on ([#414](https://github.com/sokolaidev/maf-extensions/issues/414)) ([672c9b2](https://github.com/sokolaidev/maf-extensions/commit/672c9b2fcdf7b94fd0c37d7c225f66b909a259a1))


### Bug Fixes

* require maf-sandbox 0.16.0 in the dependents and 0.16 in the samples, and admit 0.17 ([#386](https://github.com/sokolaidev/maf-extensions/issues/386)) ([7133401](https://github.com/sokolaidev/maf-extensions/commit/713340192dbc710c9c18f498a6615fc401332682))
* **wslc:** make the backend satisfy the Sandbox protocol ([#370](https://github.com/sokolaidev/maf-extensions/issues/370)) ([#408](https://github.com/sokolaidev/maf-extensions/issues/408)) ([2d2221a](https://github.com/sokolaidev/maf-extensions/commit/2d2221a708cca7bdbc8e841816aa96c147c68e7b))

## [0.6.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.6.2...maf-sandbox-wslc-v0.6.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.6.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.6.1...maf-sandbox-wslc-v0.6.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.6.0...maf-sandbox-wslc-v0.6.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.7...maf-sandbox-wslc-v0.6.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

## [0.5.7](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.6...maf-sandbox-wslc-v0.5.7) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.5.6](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.5...maf-sandbox-wslc-v0.5.6) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.5.5](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.4...maf-sandbox-wslc-v0.5.5) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.5.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.3...maf-sandbox-wslc-v0.5.4) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.5.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.2...maf-sandbox-wslc-v0.5.3) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.5.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.1...maf-sandbox-wslc-v0.5.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.5.0...maf-sandbox-wslc-v0.5.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.6.0 in the dependents' range ([#150](https://github.com/sokolaidev/maf-extensions/issues/150)) ([f2e2ca1](https://github.com/sokolaidev/maf-extensions/commit/f2e2ca13d447f5885dc6d2b2d0b8d2f3e5bbb206))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.4.0...maf-sandbox-wslc-v0.5.0) (2026-08-10)


### Features

* carry raw bytes through the wslc runner seam ([#130](https://github.com/sokolaidev/maf-extensions/issues/130)) ([375b7dc](https://github.com/sokolaidev/maf-extensions/commit/375b7dca990a0f13e111751d952aea859a52d7df))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.3.0...maf-sandbox-wslc-v0.4.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96))

### Features

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96)) ([b5990ee](https://github.com/sokolaidev/maf-extensions/commit/b5990ee492bca09a0e267172216c087be1db647a))


### Bug Fixes

* admit maf-sandbox 0.4.0 in the dependents' range ([#92](https://github.com/sokolaidev/maf-extensions/issues/92)) ([101dccb](https://github.com/sokolaidev/maf-extensions/commit/101dccbcf4178d7155d646361d1ea3422cac6f7f))
* require maf-sandbox 0.5.0 in the packages that use it ([#102](https://github.com/sokolaidev/maf-extensions/issues/102)) ([cd19b00](https://github.com/sokolaidev/maf-extensions/commit/cd19b0051254e32683dfa07580506e44fb71f41a))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.2.1...maf-sandbox-wslc-v0.3.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87))

### Bug Fixes

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87)) ([fa321cf](https://github.com/sokolaidev/maf-extensions/commit/fa321cf53f643f9e30df910fc8e46c6a938d6605))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.2.0...maf-sandbox-wslc-v0.2.1) (2026-08-09)


### Bug Fixes

* admit maf-sandbox 0.3.0 in the dependents' range ([#78](https://github.com/sokolaidev/maf-extensions/issues/78)) ([89ccab0](https://github.com/sokolaidev/maf-extensions/commit/89ccab01cb4485f8d13ed1b75ae46b074f2afff2))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.1.0...maf-sandbox-wslc-v0.2.0) (2026-08-09)


### Features

* **wslc:** allowlist egress via an internal network and a filtering proxy ([#63](https://github.com/sokolaidev/maf-extensions/issues/63)) ([4641956](https://github.com/sokolaidev/maf-extensions/commit/46419565112b0e7727744b826f99d7ff04d28e37))

## [0.1.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.0.1...maf-sandbox-wslc-v0.1.0) (2026-08-08)


### Features

* a developer-machine sandbox backend on WSL containers ([#50](https://github.com/sokolaidev/maf-extensions/issues/50)) ([f073d96](https://github.com/sokolaidev/maf-extensions/commit/f073d96c4542c42b28a4099cb2bd0f19eb3a5d1e))

## Changelog

All notable changes to `maf-sandbox-wslc` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.
