# Changelog

All notable changes to `maf-sandbox-acas` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

Releases up to and including `0.2.3` were published as **`maf-sandbox-aca`**, and the entries below name it as it was — their tags and compare links point at real history and are left as they were written. `maf-sandbox-aca` is not maintained past `0.2.3`; PyPI names cannot be reused, so the rename is a new distribution rather than a continuation of that one.

## [0.18.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.18.0...maf-sandbox-acas-v0.18.1) (2026-09-06)


### Fixes

* admit maf-sandbox 0.35 in the dependents' range, and require 0.34 in the samples ([#930](https://github.com/sokolaidev/maf-extensions/issues/930)) ([4293f12](https://github.com/sokolaidev/maf-extensions/commit/4293f1224533f5074eb204321af6824b03268691))

## [0.18.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.17.1...maf-sandbox-acas-v0.18.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* require maf-sandbox 0.33.0 in the dependents and 0.33 in the samples, and do not admit 0.34 ([#909](https://github.com/sokolaidev/maf-extensions/issues/909))

### Features

* require maf-sandbox 0.33.0 in the dependents and 0.33 in the samples, and do not admit 0.34 ([#909](https://github.com/sokolaidev/maf-extensions/issues/909)) ([51fb831](https://github.com/sokolaidev/maf-extensions/commit/51fb831f7560f08571c8279e6259badc3cfe0675))

## [0.17.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.17.0...maf-sandbox-acas-v0.17.1) (2026-09-04)


### Fixes

* admit maf-sandbox 0.32 in the backends' range ([#880](https://github.com/sokolaidev/maf-extensions/issues/880)) ([fe9f543](https://github.com/sokolaidev/maf-extensions/commit/fe9f543d7cf31e7475178935e97f11557c869155))

## [0.17.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.16.0...maf-sandbox-acas-v0.17.0) (2026-09-02)


### ⚠ BREAKING CHANGES

* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789))

### Features

* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789)) ([670a005](https://github.com/sokolaidev/maf-extensions/commit/670a0055f180c83aae50179055dd84b01bbca0f5))

## [0.16.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.15.1...maf-sandbox-acas-v0.16.0) (2026-09-01)


### Features

* acas and docker use core's bundles for confinement ([#790](https://github.com/sokolaidev/maf-extensions/issues/790)) ([d0ca685](https://github.com/sokolaidev/maf-extensions/commit/d0ca68589f8f73dc4e88b72dc4a9bb0104631bfa))

## [0.15.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.15.0...maf-sandbox-acas-v0.15.1) (2026-09-01)


### Fixes

* acas and docker leave the deprecated confinement spellings, so a core release can remove them ([#781](https://github.com/sokolaidev/maf-extensions/issues/781)) ([3327de9](https://github.com/sokolaidev/maf-extensions/commit/3327de9d5dc0d0a019c71d878a80a86b806d1dcf))
* every dependent admits maf-sandbox 0.29, and the samples floor on 0.28 ([#779](https://github.com/sokolaidev/maf-extensions/issues/779)) ([71c917a](https://github.com/sokolaidev/maf-extensions/commit/71c917a8d7ae9e253a30cb38e2fb25c393332fc1))

## [0.15.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.14.0...maf-sandbox-acas-v0.15.0) (2026-08-29)


### ⚠ BREAKING CHANGES

* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737))
* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681))

### Features

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681)) ([739481d](https://github.com/sokolaidev/maf-extensions/commit/739481df3b7903c7f0015fee81282027666bc1ba))
* **sandbox-acas:** adding file deletion capability ([#709](https://github.com/sokolaidev/maf-extensions/issues/709)) ([c61efb7](https://github.com/sokolaidev/maf-extensions/commit/c61efb7ebbf487b1af6e49c33f50c8f8cb612a83))
* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737)) ([934c7e4](https://github.com/sokolaidev/maf-extensions/commit/934c7e48bc7731eb2b67ca81f5c6cae46c262467))


### Fixes

* **acas:** refuse a non-root image for FILES_OUT and HOST_TOOLS, warn for EXEC ([#724](https://github.com/sokolaidev/maf-extensions/issues/724)) ([d9f4ffa](https://github.com/sokolaidev/maf-extensions/commit/d9f4ffa35b78cca52511ae65dde21835513f0f2f))
* admit maf-sandbox 0.27 in the dependents' range, and require 0.26 in the samples ([#748](https://github.com/sokolaidev/maf-extensions/issues/748)) ([f905461](https://github.com/sokolaidev/maf-extensions/commit/f9054614f061ffacf53abbbc174501f2d5be5a74))
* require maf-sandbox 0.27.0 in the dependents and 0.27 in the samples, and admit 0.28 ([#751](https://github.com/sokolaidev/maf-extensions/issues/751)) ([49d2a75](https://github.com/sokolaidev/maf-extensions/commit/49d2a758f60f3ffd503e5a94ea2b082c4d36ce9e))
* **sandbox-acas:** reclaim removes through the data plane ([#707](https://github.com/sokolaidev/maf-extensions/issues/707)) ([96c7e4e](https://github.com/sokolaidev/maf-extensions/commit/96c7e4eb6d268048eb39ebe69486350c6e7c256c))


### Documentation

* **sandbox:** confinement is the file name check and the filesystem path check, and "walk" is retired ([#740](https://github.com/sokolaidev/maf-extensions/issues/740)) ([52ead17](https://github.com/sokolaidev/maf-extensions/commit/52ead1719d3cb523ce67ef58f38115b558a830e8))

## [0.14.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.13.1...maf-sandbox-acas-v0.14.0) (2026-08-26)


### ⚠ BREAKING CHANGES

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678))

### Features

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678)) ([3b14292](https://github.com/sokolaidev/maf-extensions/commit/3b14292c96508e89c182cd760070009f2262867b))


### Fixes

* require maf-sandbox 0.25.0 in the dependents and 0.25 in the samples, and admit 0.26 ([#690](https://github.com/sokolaidev/maf-extensions/issues/690)) ([07f4a03](https://github.com/sokolaidev/maf-extensions/commit/07f4a0316acc74b0dc9a71f15dc4b9be943922bd))

## [0.13.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.13.0...maf-sandbox-acas-v0.13.1) (2026-08-25)


### Fixes

* require maf-sandbox 0.24.0 in the dependents and 0.24 in the samples, and admit 0.25 ([#665](https://github.com/sokolaidev/maf-extensions/issues/665)) ([b410d73](https://github.com/sokolaidev/maf-extensions/commit/b410d73ac866f2abd19cf3e550f60f26920d5344))

## [0.13.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.12.2...maf-sandbox-acas-v0.13.0) (2026-08-24)


### ⚠ BREAKING CHANGES

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609))

### Features

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609)) ([6fcfcf6](https://github.com/sokolaidev/maf-extensions/commit/6fcfcf6259874bbcb4f02ac23bddcc92ae6d8550))


### Fixes

* require maf-sandbox 0.22.0 in the dependents and 0.22 in the samples, and admit 0.23 ([#619](https://github.com/sokolaidev/maf-extensions/issues/619)) ([d8e122a](https://github.com/sokolaidev/maf-extensions/commit/d8e122a8f67e710704a4ffa0c11fdbebdaefb84e))
* require maf-sandbox 0.23.1 in the dependents and 0.23 in the samples, and admit 0.24 ([#652](https://github.com/sokolaidev/maf-extensions/issues/652)) ([f03d7f0](https://github.com/sokolaidev/maf-extensions/commit/f03d7f06d48a44079bc53d57337b06c5440870ae))

## [0.12.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.12.1...maf-sandbox-acas-v0.12.2) (2026-08-23)


### Fixes

* require maf-sandbox 0.21.0 in the dependents and 0.21 in the samples, and admit 0.22 ([#596](https://github.com/sokolaidev/maf-extensions/issues/596)) ([1028a57](https://github.com/sokolaidev/maf-extensions/commit/1028a57e16d2fe5cb3aa0b3b948680e52fce90c3))

## [0.12.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.12.0...maf-sandbox-acas-v0.12.1) (2026-08-22)


### Fixes

* require maf-sandbox 0.20.0 in the dependents and 0.20 in the samples, and admit 0.21 ([#564](https://github.com/sokolaidev/maf-extensions/issues/564)) ([727af26](https://github.com/sokolaidev/maf-extensions/commit/727af26c6db27a0de11a901d531f7183fda8426d))

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.11.0...maf-sandbox-acas-v0.12.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))
* **backends:** answer run_code on every shipped backend ([#531](https://github.com/sokolaidev/maf-extensions/issues/531)) ([7bf3cd2](https://github.com/sokolaidev/maf-extensions/commit/7bf3cd2048b7c6f41d2b1b14c79f52753f3c1db8))


### Fixes

* require maf-sandbox 0.19.0 in the dependents and 0.19 in the samples, and admit 0.20 ([#540](https://github.com/sokolaidev/maf-extensions/issues/540)) ([ae825c2](https://github.com/sokolaidev/maf-extensions/commit/ae825c2c8fd5e105402470c788b24371a77efa7c))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.10.1...maf-sandbox-acas-v0.11.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))


### Fixes

* require maf-sandbox 0.18.0 in the dependents and 0.18 in the samples, and admit 0.19 ([#494](https://github.com/sokolaidev/maf-extensions/issues/494)) ([dd12d77](https://github.com/sokolaidev/maf-extensions/commit/dd12d7745b526052268b2124803e549b1e8c3d7f))

## [0.10.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.10.0...maf-sandbox-acas-v0.10.1) (2026-08-19)


### Fixes

* **acas:** refuse a directory without recursive — the service accepted an empty one ([#474](https://github.com/sokolaidev/maf-extensions/issues/474)) ([0587fcd](https://github.com/sokolaidev/maf-extensions/commit/0587fcda00f75b134bb33bbe730db11c05994a16))

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.9.0...maf-sandbox-acas-v0.10.0) (2026-08-19)


### ⚠ BREAKING CHANGES

* **protocol:** `Sandbox` gains `remove(path, *, working_directory, recursive=False)`. An implementation that does not define it no longer satisfies the protocol. Backends that cannot confine a removal should raise `NotImplementedError` and not declare `Capability.FILES_DELETE`, as `maf-sandbox-wslc` does.

### Features

* **acas:** a bare image name boots what the service prebuilt, so a host imports nothing ([#424](https://github.com/sokolaidev/maf-extensions/issues/424)) ([f921132](https://github.com/sokolaidev/maf-extensions/commit/f921132da1beab8a246fa62d23de1c92b73a4a00))
* **protocol:** a sandbox can be asked to delete what a workload put there ([#452](https://github.com/sokolaidev/maf-extensions/issues/452)) ([2453820](https://github.com/sokolaidev/maf-extensions/commit/245382036ba1e2ddc18dea79b8e97d2cfb561935))
* **sandbox:** probes for every capability a backend claims, and CI that enumerates backends rather than listing them ([#462](https://github.com/sokolaidev/maf-extensions/issues/462)) ([f0915c7](https://github.com/sokolaidev/maf-extensions/commit/f0915c71819c729cd33aa130749fffc8d69fa377))


### Fixes

* require maf-sandbox 0.17.0 in the dependents and 0.17 in the samples, and admit 0.18 ([#472](https://github.com/sokolaidev/maf-extensions/issues/472)) ([dffd936](https://github.com/sokolaidev/maf-extensions/commit/dffd936ed3cb3c6a49d1dce0776ba321ee4d1dda))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.8.3...maf-sandbox-acas-v0.9.0) (2026-08-17)


### Features

* **acas:** declare HOST_TOOLS, so a kind wiring a host-tool registry attaches here too ([#418](https://github.com/sokolaidev/maf-extensions/issues/418)) ([b8b90e0](https://github.com/sokolaidev/maf-extensions/commit/b8b90e0e09a16afb25d4da13d8038c0b7d7b2338))
* **backends:** each backend exports the name `selected=` matches on ([#414](https://github.com/sokolaidev/maf-extensions/issues/414)) ([672c9b2](https://github.com/sokolaidev/maf-extensions/commit/672c9b2fcdf7b94fd0c37d7c225f66b909a259a1))


### Bug Fixes

* require maf-sandbox 0.16.0 in the dependents and 0.16 in the samples, and admit 0.17 ([#386](https://github.com/sokolaidev/maf-extensions/issues/386)) ([7133401](https://github.com/sokolaidev/maf-extensions/commit/713340192dbc710c9c18f498a6615fc401332682))

## [0.8.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.8.2...maf-sandbox-acas-v0.8.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.8.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.8.1...maf-sandbox-acas-v0.8.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.8.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.8.0...maf-sandbox-acas-v0.8.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.7.1...maf-sandbox-acas-v0.8.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

## [0.7.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.7.0...maf-sandbox-acas-v0.7.1) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.6.4...maf-sandbox-acas-v0.7.0) (2026-08-12)


### Features

* name a symlink in the protocol, and hold every FILES_OUT backend to one conformance suite ([#215](https://github.com/sokolaidev/maf-extensions/issues/215)) ([051c4c2](https://github.com/sokolaidev/maf-extensions/commit/051c4c2a906dcca4f5bd45654834061befc71308))


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.6.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.6.3...maf-sandbox-acas-v0.6.4) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.6.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.6.2...maf-sandbox-acas-v0.6.3) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.6.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.6.1...maf-sandbox-acas-v0.6.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.6.0...maf-sandbox-acas-v0.6.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.5.0...maf-sandbox-acas-v0.6.0) (2026-08-11)


### Features

* ACAS reads artifacts out, refusing symlinks the SDK cannot even see ([#139](https://github.com/sokolaidev/maf-extensions/issues/139)) ([e989721](https://github.com/sokolaidev/maf-extensions/commit/e989721a538a85a56b17129d761b6d5092d261a5))


### Documentation

* stop claiming a regularity guarantee ACAS cannot keep ([#147](https://github.com/sokolaidev/maf-extensions/issues/147)) ([e328daf](https://github.com/sokolaidev/maf-extensions/commit/e328dafe513047e6d68de9542754562dff9e3601))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.4.0...maf-sandbox-acas-v0.5.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96))

### Features

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96)) ([b5990ee](https://github.com/sokolaidev/maf-extensions/commit/b5990ee492bca09a0e267172216c087be1db647a))


### Bug Fixes

* admit maf-sandbox 0.4.0 in the dependents' range ([#92](https://github.com/sokolaidev/maf-extensions/issues/92)) ([101dccb](https://github.com/sokolaidev/maf-extensions/commit/101dccbcf4178d7155d646361d1ea3422cac6f7f))
* require maf-sandbox 0.5.0 in the packages that use it ([#102](https://github.com/sokolaidev/maf-extensions/issues/102)) ([cd19b00](https://github.com/sokolaidev/maf-extensions/commit/cd19b0051254e32683dfa07580506e44fb71f41a))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.3.2...maf-sandbox-acas-v0.4.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87))

### Bug Fixes

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87)) ([fa321cf](https://github.com/sokolaidev/maf-extensions/commit/fa321cf53f643f9e30df910fc8e46c6a938d6605))

## [0.3.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.3.1...maf-sandbox-acas-v0.3.2) (2026-08-09)


### Documentation

* ship the bicep-sandbox image and the commands that deploy it ([#82](https://github.com/sokolaidev/maf-extensions/issues/82)) ([5fa8829](https://github.com/sokolaidev/maf-extensions/commit/5fa88294a8e74b6eba7f1c9d061acd821d559887))

## [0.3.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-acas-v0.3.0...maf-sandbox-acas-v0.3.1) (2026-08-09)


### Bug Fixes

* admit maf-sandbox 0.3.0 in the dependents' range ([#78](https://github.com/sokolaidev/maf-extensions/issues/78)) ([89ccab0](https://github.com/sokolaidev/maf-extensions/commit/89ccab01cb4485f8d13ed1b75ae46b074f2afff2))

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
