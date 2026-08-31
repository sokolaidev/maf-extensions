# Changelog

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.11.0...maf-sandbox-docker-v0.12.0) (2026-08-31)


### Features

* **sandbox:** core offers a container-cp tar header helper, and two backends (docker & wslc) use it ([#766](https://github.com/sokolaidev/maf-extensions/issues/766)) ([f34218e](https://github.com/sokolaidev/maf-extensions/commit/f34218ef599301bfacf7fa99ef62c82fd9c164b5))
* **sandbox:** the check that licenses as-root file removals moves from the docker backend into maf-sandbox ([#770](https://github.com/sokolaidev/maf-extensions/issues/770)) ([53820a8](https://github.com/sokolaidev/maf-extensions/commit/53820a84ca23f24a3af311c809f2110eb7e2edad))


### Fixes

* **docker:** hold the maf-sandbox ceiling one minor below the bump script's target ([#778](https://github.com/sokolaidev/maf-extensions/issues/778)) ([366f3bc](https://github.com/sokolaidev/maf-extensions/commit/366f3bc89e591236f7b17f757216a8b3fbe92770))
* **docker:** require maf-sandbox 0.28.0, the release that carries the helpers this backend calls ([#776](https://github.com/sokolaidev/maf-extensions/issues/776)) ([10700db](https://github.com/sokolaidev/maf-extensions/commit/10700db1b59c51bb4dd3755d6e8f8fea10c824bf))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.10.0...maf-sandbox-docker-v0.11.0) (2026-08-30)


### Features

* **docker:** the daemon says which guest it runs, so the backend declares it ([#587](https://github.com/sokolaidev/maf-extensions/issues/587)) ([#747](https://github.com/sokolaidev/maf-extensions/issues/747)) ([7e78f1b](https://github.com/sokolaidev/maf-extensions/commit/7e78f1b9d825f7222e54a65807d0741a17eba6db))

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.9.0...maf-sandbox-docker-v0.10.0) (2026-08-29)


### ⚠ BREAKING CHANGES

* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737))
* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681))

### Features

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681)) ([739481d](https://github.com/sokolaidev/maf-extensions/commit/739481df3b7903c7f0015fee81282027666bc1ba))
* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737)) ([934c7e4](https://github.com/sokolaidev/maf-extensions/commit/934c7e48bc7731eb2b67ca81f5c6cae46c262467))


### Fixes

* admit maf-sandbox 0.27 in the dependents' range, and require 0.26 in the samples ([#748](https://github.com/sokolaidev/maf-extensions/issues/748)) ([f905461](https://github.com/sokolaidev/maf-extensions/commit/f9054614f061ffacf53abbbc174501f2d5be5a74))
* **backends:** a scope purge subtracts nothing from the retry record ([#705](https://github.com/sokolaidev/maf-extensions/issues/705)) ([6fdeb7a](https://github.com/sokolaidev/maf-extensions/commit/6fdeb7a02f308dd1adc5040413859928c3cf197f))
* **docker:** raise authority for a removal only over a path the guest could not have swapped ([#684](https://github.com/sokolaidev/maf-extensions/issues/684)) ([b2c85fb](https://github.com/sokolaidev/maf-extensions/commit/b2c85fb1d576142df2eac50f140e6e0d53367321))
* **docker:** write_file lands under the image user, not root ([#680](https://github.com/sokolaidev/maf-extensions/issues/680)) ([#719](https://github.com/sokolaidev/maf-extensions/issues/719)) ([be4212f](https://github.com/sokolaidev/maf-extensions/commit/be4212fed676d5b243c34a225d94ce5719667751))
* require maf-sandbox 0.27.0 in the dependents and 0.27 in the samples, and admit 0.28 ([#751](https://github.com/sokolaidev/maf-extensions/issues/751)) ([49d2a75](https://github.com/sokolaidev/maf-extensions/commit/49d2a758f60f3ffd503e5a94ea2b082c4d36ce9e))


### Documentation

* **sandbox:** confinement is the file name check and the filesystem path check, and "walk" is retired ([#740](https://github.com/sokolaidev/maf-extensions/issues/740)) ([52ead17](https://github.com/sokolaidev/maf-extensions/commit/52ead1719d3cb523ce67ef58f38115b558a830e8))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.8.2...maf-sandbox-docker-v0.9.0) (2026-08-26)


### ⚠ BREAKING CHANGES

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678))

### Features

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678)) ([3b14292](https://github.com/sokolaidev/maf-extensions/commit/3b14292c96508e89c182cd760070009f2262867b))


### Fixes

* require maf-sandbox 0.25.0 in the dependents and 0.25 in the samples, and admit 0.26 ([#690](https://github.com/sokolaidev/maf-extensions/issues/690)) ([07f4a03](https://github.com/sokolaidev/maf-extensions/commit/07f4a0316acc74b0dc9a71f15dc4b9be943922bd))

## [0.8.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.8.1...maf-sandbox-docker-v0.8.2) (2026-08-25)


### Fixes

* require maf-sandbox 0.24.0 in the dependents and 0.24 in the samples, and admit 0.25 ([#665](https://github.com/sokolaidev/maf-extensions/issues/665)) ([b410d73](https://github.com/sokolaidev/maf-extensions/commit/b410d73ac866f2abd19cf3e550f60f26920d5344))

## [0.8.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.8.0...maf-sandbox-docker-v0.8.1) (2026-08-24)


### Fixes

* require maf-sandbox 0.23.1 in the dependents and 0.23 in the samples, and admit 0.24 ([#652](https://github.com/sokolaidev/maf-extensions/issues/652)) ([f03d7f0](https://github.com/sokolaidev/maf-extensions/commit/f03d7f06d48a44079bc53d57337b06c5440870ae))


### Documentation

* the four versions tagged on 24 August never reached PyPI ([#646](https://github.com/sokolaidev/maf-extensions/issues/646)) ([2d35b50](https://github.com/sokolaidev/maf-extensions/commit/2d35b504c9b3e6f84943ef8fba4a9dd92a2c303c))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.7.3...maf-sandbox-docker-v0.8.0) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox-docker` 0.8.0 to install. The publish run failed before the upload, on a repository test that read the tags of a shallow checkout ([#645](https://github.com/sokolaidev/maf-extensions/pull/645)); the tag records the right commit and no artifact was ever built from it.
>
> Release tags here cannot be moved, so this version number is spent rather than reused. **The code these entries describe ships in 0.8.1**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### ⚠ BREAKING CHANGES

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609))

### Features

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609)) ([6fcfcf6](https://github.com/sokolaidev/maf-extensions/commit/6fcfcf6259874bbcb4f02ac23bddcc92ae6d8550))


### Fixes

* require maf-sandbox 0.22.0 in the dependents and 0.22 in the samples, and admit 0.23 ([#619](https://github.com/sokolaidev/maf-extensions/issues/619)) ([d8e122a](https://github.com/sokolaidev/maf-extensions/commit/d8e122a8f67e710704a4ffa0c11fdbebdaefb84e))

## [0.7.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.7.2...maf-sandbox-docker-v0.7.3) (2026-08-23)


### Fixes

* require maf-sandbox 0.21.0 in the dependents and 0.21 in the samples, and admit 0.22 ([#596](https://github.com/sokolaidev/maf-extensions/issues/596)) ([1028a57](https://github.com/sokolaidev/maf-extensions/commit/1028a57e16d2fe5cb3aa0b3b948680e52fce90c3))

## [0.7.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.7.1...maf-sandbox-docker-v0.7.2) (2026-08-22)


### Fixes

* require maf-sandbox 0.20.0 in the dependents and 0.20 in the samples, and admit 0.21 ([#564](https://github.com/sokolaidev/maf-extensions/issues/564)) ([727af26](https://github.com/sokolaidev/maf-extensions/commit/727af26c6db27a0de11a901d531f7183fda8426d))

## [0.7.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.7.0...maf-sandbox-docker-v0.7.1) (2026-08-22)


### Documentation

* say what the 0.19 egress change breaks, and what to do about it ([#543](https://github.com/sokolaidev/maf-extensions/issues/543)) ([4b736de](https://github.com/sokolaidev/maf-extensions/commit/4b736de3368feb7fca0ffe36f2b607214d376d44))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.6.0...maf-sandbox-docker-v0.7.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))
* **backends:** answer run_code on every shipped backend ([#531](https://github.com/sokolaidev/maf-extensions/issues/531)) ([7bf3cd2](https://github.com/sokolaidev/maf-extensions/commit/7bf3cd2048b7c6f41d2b1b14c79f52753f3c1db8))


### Fixes

* require maf-sandbox 0.19.0 in the dependents and 0.19 in the samples, and admit 0.20 ([#540](https://github.com/sokolaidev/maf-extensions/issues/540)) ([ae825c2](https://github.com/sokolaidev/maf-extensions/commit/ae825c2c8fd5e105402470c788b24371a77efa7c))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-v0.5.0...maf-sandbox-docker-v0.6.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))


### Fixes

* require maf-sandbox 0.18.0 in the dependents and 0.18 in the samples, and admit 0.19 ([#494](https://github.com/sokolaidev/maf-extensions/issues/494)) ([dd12d77](https://github.com/sokolaidev/maf-extensions/commit/dd12d7745b526052268b2124803e549b1e8c3d7f))

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
