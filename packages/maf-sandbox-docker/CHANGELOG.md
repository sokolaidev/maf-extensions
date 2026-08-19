# Changelog

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.4.0...maf-sandbox-docker-v0.5.0) (2026-08-19)


### ⚠ BREAKING CHANGES

* **protocol:** `Sandbox` gains `remove(path, *, working_directory, recursive=False)`. An implementation that does not define it no longer satisfies the protocol. Backends that cannot confine a removal should raise `NotImplementedError` and not declare `Capability.FILES_DELETE`, as `maf-sandbox-wslc` does.

### Features

* **protocol:** a sandbox can be asked to delete what a workload put there ([#452](https://github.com/sokolaidev/maf-extensions/issues/452)) ([2453820](https://github.com/sokolaidev/maf-extensions/commit/245382036ba1e2ddc18dea79b8e97d2cfb561935))
* **sandbox:** probes for every capability a backend claims, and CI that enumerates backends rather than listing them ([#462](https://github.com/sokolaidev/maf-extensions/issues/462)) ([f0915c7](https://github.com/sokolaidev/maf-extensions/commit/f0915c71819c729cd33aa130749fffc8d69fa377))


### Fixes

* require maf-sandbox 0.17.0 in the dependents and 0.17 in the samples, and admit 0.18 ([#472](https://github.com/sokolaidev/maf-extensions/issues/472)) ([dffd936](https://github.com/sokolaidev/maf-extensions/commit/dffd936ed3cb3c6a49d1dce0776ba321ee4d1dda))


### Documentation

* **docker:** remove records the walk-then-act residual itself ([#460](https://github.com/sokolaidev/maf-extensions/issues/460)) ([282cd96](https://github.com/sokolaidev/maf-extensions/commit/282cd9611bc45cac1011b73fe63b31597d97f605))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.3.3...maf-sandbox-docker-v0.4.0) (2026-08-17)


### Features

* **backends:** each backend exports the name `selected=` matches on ([#414](https://github.com/sokolaidev/maf-extensions/issues/414)) ([672c9b2](https://github.com/sokolaidev/maf-extensions/commit/672c9b2fcdf7b94fd0c37d7c225f66b909a259a1))
* **docker:** declare HOST_TOOLS, so a kind wiring a host-tool registry attaches here ([#410](https://github.com/sokolaidev/maf-extensions/issues/410)) ([b7a3fa2](https://github.com/sokolaidev/maf-extensions/commit/b7a3fa2bad747a1c365e2e177ed020f41f40ece8))


### Bug Fixes

* **docker:** an empty egress_proxy_image is no proxy configured, as the declaration already said ([#419](https://github.com/sokolaidev/maf-extensions/issues/419)) ([d40ddf7](https://github.com/sokolaidev/maf-extensions/commit/d40ddf72914e58b6d905d9677489d1db2c6d4c68))
* require maf-sandbox 0.16.0 in the dependents and 0.16 in the samples, and admit 0.17 ([#386](https://github.com/sokolaidev/maf-extensions/issues/386)) ([7133401](https://github.com/sokolaidev/maf-extensions/commit/713340192dbc710c9c18f498a6615fc401332682))

## [0.3.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.3.2...maf-sandbox-docker-v0.3.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.3.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.3.1...maf-sandbox-docker-v0.3.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.3.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.3.0...maf-sandbox-docker-v0.3.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.2.1...maf-sandbox-docker-v0.3.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* **docker:** stop _remove_network warning on a network that was never there ([#309](https://github.com/sokolaidev/maf-extensions/issues/309)) ([d634df4](https://github.com/sokolaidev/maf-extensions/commit/d634df40e3beef24a4e56733e5e564dfae5637ba))
* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.2.0...maf-sandbox-docker-v0.2.1) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.1.4...maf-sandbox-docker-v0.2.0) (2026-08-12)


### Features

* name a symlink in the protocol, and hold every FILES_OUT backend to one conformance suite ([#215](https://github.com/sokolaidev/maf-extensions/issues/215)) ([051c4c2](https://github.com/sokolaidev/maf-extensions/commit/051c4c2a906dcca4f5bd45654834061befc71308))


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.1.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.1.3...maf-sandbox-docker-v0.1.4) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.1.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.1.2...maf-sandbox-docker-v0.1.3) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.1.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.1.1...maf-sandbox-docker-v0.1.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.1.0...maf-sandbox-docker-v0.1.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.1.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.0.1...maf-sandbox-docker-v0.1.0) (2026-08-11)


### Features

* maf-sandbox-docker — plain Docker containers as a local and CI backend, reading files out ([#129](https://github.com/sokolaidev/maf-extensions/issues/129)) ([5a8f641](https://github.com/sokolaidev/maf-extensions/commit/5a8f641a9a1716fff136d4a1f2bc48d548d80440))


### Bug Fixes

* refuse a symlinked parent in the Docker backend, which escaped confinement ([#143](https://github.com/sokolaidev/maf-extensions/issues/143)) ([696b39d](https://github.com/sokolaidev/maf-extensions/commit/696b39d34d8041072d77321004a86673a90c43dd))

## Changelog
