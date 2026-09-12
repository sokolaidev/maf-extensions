# Changelog

## [0.21.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.21.0...maf-sandbox-wslc-v0.21.1) (2026-09-12)

> **Added after the release.** No wheel for 0.21.0 reached PyPI, so this is the version that first carries everything in the 0.21.0 section below — including its breaking change. Those entries are left where release-please wrote them rather than copied up; this version's release notes on GitHub list them in full.

### Fixes

* require maf-sandbox 0.39.0 in the dependents, and admit the 0.39 line ([#1179](https://github.com/sokolaidev/maf-extensions/issues/1179)) ([8475d6b](https://github.com/sokolaidev/maf-extensions/commit/8475d6bb3fbd4166f8e062eecb9fa35bb9fb5910))

## [0.21.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.20.0...maf-sandbox-wslc-v0.21.0) (2026-09-12)

> **Correction, added after the release.** **No wheel for this version reached PyPI.** The publish failed after release-please had already created the tag and the GitHub Release, both immutable here, so the number is spent. Everything listed below is published as **0.21.1** instead — including the breaking change, which therefore reaches consumers under a patch version.
>
> The failing step was *The release's own packages install together*. `maf-sandbox-codeact` had already moved its core range onto the `maf-sandbox` 0.39 line while every other package in the suite still required `>=0.38.0,<0.39`, so the seven wheels this release built could not resolve into one environment. [#1179](https://github.com/sokolaidev/maf-extensions/pull/1179) unified the ranges, and merged after this release had already been cut.
>
> Left in place rather than deleted: the generated entries are the honest record of what release-please saw. They are accurate about what this repository released; they are wrong only about what the index carries.

### ⚠ BREAKING CHANGES

* **sandbox:** an egress allow entry is one hostname, and a bare "*" no longer allows every host ([#1138](https://github.com/sokolaidev/maf-extensions/issues/1138))

### Features

* **docker+wslc+acas:** a workload can ask for a sandbox per tool call, and all three backends serve one ([#1139](https://github.com/sokolaidev/maf-extensions/issues/1139)) ([e6cf30d](https://github.com/sokolaidev/maf-extensions/commit/e6cf30d8f85da4b01fdfcd455228b3cf0e2b8fee))


### Fixes

* **docker+wslc:** a disposal files a swept proxy's egress window under the key that ran behind it ([#1149](https://github.com/sokolaidev/maf-extensions/issues/1149)) ([3b16358](https://github.com/sokolaidev/maf-extensions/commit/3b16358c84d202c3bce36dbb18c304556583d4e2))
* **sandbox:** an egress allow entry is one hostname, and a bare "*" no longer allows every host ([#1138](https://github.com/sokolaidev/maf-extensions/issues/1138)) ([aa3edba](https://github.com/sokolaidev/maf-extensions/commit/aa3edba601ce025bc8a5971b9aa942eeaa9ef557))
* **wslc:** remove temporary host copies after path checks ([#1157](https://github.com/sokolaidev/maf-extensions/issues/1157)) ([216652a](https://github.com/sokolaidev/maf-extensions/commit/216652a249a87a6a778f5d6833d5507278405965))
* **wslc:** the engine answers the write-path check, and a guest cannot claim a directory ([#1135](https://github.com/sokolaidev/maf-extensions/issues/1135)) ([8ee28bc](https://github.com/sokolaidev/maf-extensions/commit/8ee28bc0785b3e33cb351974d8c7ca6a3b9e1e8f))

## [0.20.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.19.1...maf-sandbox-wslc-v0.20.0) (2026-09-11)


### ⚠ BREAKING CHANGES

* **sandbox:** preserve exec bytes and expose safe text views ([#1100](https://github.com/sokolaidev/maf-extensions/issues/1100))
* **sandbox:** dispose sandboxes by default and audit process cleanup ([#1091](https://github.com/sokolaidev/maf-extensions/issues/1091))
* **sandbox:** resolve workload paths against backend storage bases ([#1090](https://github.com/sokolaidev/maf-extensions/issues/1090))

### Features

* **sandbox:** dispose sandboxes by default and audit process cleanup ([#1091](https://github.com/sokolaidev/maf-extensions/issues/1091)) ([a11c06e](https://github.com/sokolaidev/maf-extensions/commit/a11c06e85c8114520e489ce3fce0c275c81840b8))
* **sandbox:** prepare working directories when acquiring sandboxes ([#1086](https://github.com/sokolaidev/maf-extensions/issues/1086)) ([e774a51](https://github.com/sokolaidev/maf-extensions/commit/e774a512c32097cfb3803660838545a2ecd64095))
* **sandbox:** preserve exec bytes and expose safe text views ([#1100](https://github.com/sokolaidev/maf-extensions/issues/1100)) ([69daefa](https://github.com/sokolaidev/maf-extensions/commit/69daefaff889f3a31049cdf9dcf6c707fa5ef39a))
* **sandbox:** resolve workload paths against backend storage bases ([#1090](https://github.com/sokolaidev/maf-extensions/issues/1090)) ([1d93a39](https://github.com/sokolaidev/maf-extensions/commit/1d93a39e2dc30a7f6ddc5dba17a4a4fc201ff9e9))


### Fixes

* **sandbox:** refuse missing sandbox commands during acquisition ([#1089](https://github.com/sokolaidev/maf-extensions/issues/1089)) ([80f0a16](https://github.com/sokolaidev/maf-extensions/commit/80f0a16d01cd732a0f100f694efa66af893c31cd))

## [0.19.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.19.0...maf-sandbox-wslc-v0.19.1) (2026-09-10)


### Fixes

* **docker+wslc:** avoid double-counting egress decisions after failed proxy removal ([#1070](https://github.com/sokolaidev/maf-extensions/issues/1070)) ([9daf1d3](https://github.com/sokolaidev/maf-extensions/commit/9daf1d394190040293628df02d072ff3c7287b2f))
* **docker+wslc:** preserve proxy attribution across restarts and concurrent cleanup ([#1071](https://github.com/sokolaidev/maf-extensions/issues/1071)) ([b22bfd8](https://github.com/sokolaidev/maf-extensions/commit/b22bfd865e3cae3429907e98d2bc42b263352348))

## [0.19.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.18.0...maf-sandbox-wslc-v0.19.0) (2026-09-10)


### ⚠ BREAKING CHANGES

* target cleanup and retries at the physical sandbox instance ([#1060](https://github.com/sokolaidev/maf-extensions/issues/1060))
* **sandbox:** clean unfamiliar sandbox instances before serving calls ([#1045](https://github.com/sokolaidev/maf-extensions/issues/1045))

### Features

* accept method-scoped egress policies and refuse unsupported backends ([#1063](https://github.com/sokolaidev/maf-extensions/issues/1063)) ([39fd53b](https://github.com/sokolaidev/maf-extensions/commit/39fd53b457a301d59a646d815e717b8e61b6d698))
* gate reclamation conformance and refuse unsafe WSLC cleanup ([#1036](https://github.com/sokolaidev/maf-extensions/issues/1036)) ([55fb81a](https://github.com/sokolaidev/maf-extensions/commit/55fb81a8f925c878699d23d1caa8b487b465e8f1))
* **sandbox:** clean unfamiliar sandbox instances before serving calls ([#1045](https://github.com/sokolaidev/maf-extensions/issues/1045)) ([333e6f9](https://github.com/sokolaidev/maf-extensions/commit/333e6f9e1267f3ec53f3b61477b9ea745827528a))
* target cleanup and retries at the physical sandbox instance ([#1060](https://github.com/sokolaidev/maf-extensions/issues/1060)) ([50a123f](https://github.com/sokolaidev/maf-extensions/commit/50a123f15b0e1e358a9c5d191aafb56ac6a5dcf6))


### Fixes

* **wslc:** let non-root guests modify inputs and create outputs ([#1053](https://github.com/sokolaidev/maf-extensions/issues/1053)) ([7618803](https://github.com/sokolaidev/maf-extensions/commit/761880392f49f527d855d0aaf178bff52b5827d8))

## [0.18.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.17.0...maf-sandbox-wslc-v0.18.0) (2026-09-08)


### ⚠ BREAKING CHANGES

* **sandbox:** a call leaves nothing behind by default ([#999](https://github.com/sokolaidev/maf-extensions/issues/999))

### Features

* **sandbox:** a call leaves nothing behind by default ([#999](https://github.com/sokolaidev/maf-extensions/issues/999)) ([e0ad6b9](https://github.com/sokolaidev/maf-extensions/commit/e0ad6b9c0a8516477d9f7b99afdb7724cd72677c))
* **wslc:** cleanup stopped sandboxes and orphaned infrastructure ([#1015](https://github.com/sokolaidev/maf-extensions/issues/1015)) ([2501bef](https://github.com/sokolaidev/maf-extensions/commit/2501bef9d8d5a8cd0cbd8235653fcf2f5ec6535e))


### Fixes

* require maf-sandbox 0.36.0 in the dependents, and admit the 0.36 line ([#1018](https://github.com/sokolaidev/maf-extensions/issues/1018)) ([d58e1ac](https://github.com/sokolaidev/maf-extensions/commit/d58e1ac02d0372e356cf00dbce5fe59c38b35324))

## [0.17.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.16.1...maf-sandbox-wslc-v0.17.0) (2026-09-07)


### Features

* a backend reports what its egress enforcement decided, so a record separates what a sandbox was allowed to reach from what it did ([#963](https://github.com/sokolaidev/maf-extensions/issues/963)) ([20b5f50](https://github.com/sokolaidev/maf-extensions/commit/20b5f5044076085525c2bb2a66bfa893dc1cbe55))
* acas and wslc declare the POSIX guest they hand out ([#588](https://github.com/sokolaidev/maf-extensions/issues/588)) ([#946](https://github.com/sokolaidev/maf-extensions/issues/946)) ([4109b18](https://github.com/sokolaidev/maf-extensions/commit/4109b188e8eac21e24d1496e88d65ffb07eda97a))

## [0.16.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.16.0...maf-sandbox-wslc-v0.16.1) (2026-09-06)


### Fixes

* admit maf-sandbox 0.35 in the dependents' range, and require 0.34 in the samples ([#930](https://github.com/sokolaidev/maf-extensions/issues/930)) ([4293f12](https://github.com/sokolaidev/maf-extensions/commit/4293f1224533f5074eb204321af6824b03268691))

## [0.16.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.15.1...maf-sandbox-wslc-v0.16.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* require maf-sandbox 0.33.0 in the dependents and 0.33 in the samples, and do not admit 0.34 ([#909](https://github.com/sokolaidev/maf-extensions/issues/909))

### Features

* require maf-sandbox 0.33.0 in the dependents and 0.33 in the samples, and do not admit 0.34 ([#909](https://github.com/sokolaidev/maf-extensions/issues/909)) ([51fb831](https://github.com/sokolaidev/maf-extensions/commit/51fb831f7560f08571c8279e6259badc3cfe0675))

## [0.15.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.15.0...maf-sandbox-wslc-v0.15.1) (2026-09-04)


### Fixes

* admit maf-sandbox 0.32 in the backends' range ([#880](https://github.com/sokolaidev/maf-extensions/issues/880)) ([fe9f543](https://github.com/sokolaidev/maf-extensions/commit/fe9f543d7cf31e7475178935e97f11557c869155))
* **wslc:** remove's refusal names who answers the confinement check ([#843](https://github.com/sokolaidev/maf-extensions/issues/843)) ([6dc0c8b](https://github.com/sokolaidev/maf-extensions/commit/6dc0c8b7d83ffdd3d76db98073b8ed7f28a619f9))

## [0.15.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.14.1...maf-sandbox-wslc-v0.15.0) (2026-09-02)


### ⚠ BREAKING CHANGES

* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789))

### Features

* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789)) ([670a005](https://github.com/sokolaidev/maf-extensions/commit/670a0055f180c83aae50179055dd84b01bbca0f5))

## [0.14.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.14.0...maf-sandbox-wslc-v0.14.1) (2026-09-01)


### Fixes

* every dependent admits maf-sandbox 0.29, and the samples floor on 0.28 ([#779](https://github.com/sokolaidev/maf-extensions/issues/779)) ([71c917a](https://github.com/sokolaidev/maf-extensions/commit/71c917a8d7ae9e253a30cb38e2fb25c393332fc1))

## [0.14.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.13.0...maf-sandbox-wslc-v0.14.0) (2026-09-01)


### Features

* core ships the guest-side stat ([#777](https://github.com/sokolaidev/maf-extensions/issues/777)) ([cd17235](https://github.com/sokolaidev/maf-extensions/commit/cd17235d7870cf9fa96c36867ff0a37c8f85f264))
* **sandbox:** core offers a container-cp tar header helper, and two backends (docker & wslc) use it ([#766](https://github.com/sokolaidev/maf-extensions/issues/766)) ([f34218e](https://github.com/sokolaidev/maf-extensions/commit/f34218ef599301bfacf7fa99ef62c82fd9c164b5))

## [0.13.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.12.0...maf-sandbox-wslc-v0.13.0) (2026-08-29)


### ⚠ BREAKING CHANGES

* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737))
* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681))

### Features

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681)) ([739481d](https://github.com/sokolaidev/maf-extensions/commit/739481df3b7903c7f0015fee81282027666bc1ba))
* **sandbox:** a backend's four optional declarations become one BackendDeclarations ([#737](https://github.com/sokolaidev/maf-extensions/issues/737)) ([934c7e4](https://github.com/sokolaidev/maf-extensions/commit/934c7e48bc7731eb2b67ca81f5c6cae46c262467))


### Fixes

* admit maf-sandbox 0.27 in the dependents' range, and require 0.26 in the samples ([#748](https://github.com/sokolaidev/maf-extensions/issues/748)) ([f905461](https://github.com/sokolaidev/maf-extensions/commit/f9054614f061ffacf53abbbc174501f2d5be5a74))
* **backends:** a scope purge subtracts nothing from the retry record ([#705](https://github.com/sokolaidev/maf-extensions/issues/705)) ([6fdeb7a](https://github.com/sokolaidev/maf-extensions/commit/6fdeb7a02f308dd1adc5040413859928c3cf197f))
* require maf-sandbox 0.27.0 in the dependents and 0.27 in the samples, and admit 0.28 ([#751](https://github.com/sokolaidev/maf-extensions/issues/751)) ([49d2a75](https://github.com/sokolaidev/maf-extensions/commit/49d2a758f60f3ffd503e5a94ea2b082c4d36ce9e))
* **wslc:** raise authority for reclaim on a non-root image ([#706](https://github.com/sokolaidev/maf-extensions/issues/706)) ([12eed5a](https://github.com/sokolaidev/maf-extensions/commit/12eed5af554067c6f6d4be049a600b02a85a0714))
* **wslc:** refuse to reclaim a relative path or one too close to the root ([#715](https://github.com/sokolaidev/maf-extensions/issues/715)) ([82f52f8](https://github.com/sokolaidev/maf-extensions/commit/82f52f83db028a58512b11b167d7d13367390500))


### Documentation

* **sandbox:** a confinement stat may not be answered by the guest, and wslc records that its is ([#739](https://github.com/sokolaidev/maf-extensions/issues/739)) ([e4f6566](https://github.com/sokolaidev/maf-extensions/commit/e4f65668e3cfd5d2012df52cac25c4c0d893fdab))
* **sandbox:** confinement is the file name check and the filesystem path check, and "walk" is retired ([#740](https://github.com/sokolaidev/maf-extensions/issues/740)) ([52ead17](https://github.com/sokolaidev/maf-extensions/commit/52ead1719d3cb523ce67ef58f38115b558a830e8))

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.11.3...maf-sandbox-wslc-v0.12.0) (2026-08-26)


### ⚠ BREAKING CHANGES

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678))

### Features

* **sandbox:** a failed delete comes back as a code ([#678](https://github.com/sokolaidev/maf-extensions/issues/678)) ([3b14292](https://github.com/sokolaidev/maf-extensions/commit/3b14292c96508e89c182cd760070009f2262867b))


### Fixes

* require maf-sandbox 0.25.0 in the dependents and 0.25 in the samples, and admit 0.26 ([#690](https://github.com/sokolaidev/maf-extensions/issues/690)) ([07f4a03](https://github.com/sokolaidev/maf-extensions/commit/07f4a0316acc74b0dc9a71f15dc4b9be943922bd))

## [0.11.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.11.2...maf-sandbox-wslc-v0.11.3) (2026-08-25)


### Fixes

* require maf-sandbox 0.24.0 in the dependents and 0.24 in the samples, and admit 0.25 ([#665](https://github.com/sokolaidev/maf-extensions/issues/665)) ([b410d73](https://github.com/sokolaidev/maf-extensions/commit/b410d73ac866f2abd19cf3e550f60f26920d5344))

## [0.11.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-wslc-v0.11.1...maf-sandbox-wslc-v0.11.2) (2026-08-24)


### Documentation

* wslc 0.11.1 and bicep 0.9.5 never reached PyPI either, and where their code ships ([#654](https://github.com/sokolaidev/maf-extensions/issues/654)) ([e198594](https://github.com/sokolaidev/maf-extensions/commit/e19859405ed78bdc2c356d5d8f2ceb727e44441c))

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
