# Changelog

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.5.0...maf-sandbox-docker-sbx-v0.5.1) (2026-10-06)


### Fixes

* require maf-sandbox 0.48.0 in the dependents, and admit the 0.48 line ([#1747](https://github.com/sokolaidev/maf-extensions/issues/1747)) ([0926d2f](https://github.com/sokolaidev/maf-extensions/commit/0926d2f4a6944e2755a82ce187ef219ee227192b))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.4.2...maf-sandbox-docker-sbx-v0.5.0) (2026-10-04)


### ⚠ BREAKING CHANGES

* select backend-owned program channels and confirm host-tool delivery ([#1664](https://github.com/sokolaidev/maf-extensions/issues/1664))

### Features

* select backend-owned program channels and confirm host-tool delivery ([#1664](https://github.com/sokolaidev/maf-extensions/issues/1664)) ([5d09f66](https://github.com/sokolaidev/maf-extensions/commit/5d09f6670aeddc8569ad5ce79d8c2e7f2c17b7d5))
* support host tools in Docker Sandboxes with per-run activity guards ([#1635](https://github.com/sokolaidev/maf-extensions/issues/1635)) ([2344c81](https://github.com/sokolaidev/maf-extensions/commit/2344c8115977fe3b429241055a6a48b4c0a440c4))

## [0.4.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.4.1...maf-sandbox-docker-sbx-v0.4.2) (2026-10-01)


### Fixes

* require maf-sandbox 0.46.0 in the dependents, and admit the 0.46 line ([#1619](https://github.com/sokolaidev/maf-extensions/issues/1619)) ([868fb2d](https://github.com/sokolaidev/maf-extensions/commit/868fb2d457409ccf099052333d473598db290f84))


### Documentation

* **docker-sbx:** record verified backend delivery and remaining validation ([#1616](https://github.com/sokolaidev/maf-extensions/issues/1616)) ([dd358f3](https://github.com/sokolaidev/maf-extensions/commit/dd358f3a9fbd6adafd8552f282fa238d1447cc14))

## [0.4.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.4.0...maf-sandbox-docker-sbx-v0.4.1) (2026-10-01)


### Documentation

* document closed egress for Docker Sandboxes ([#1606](https://github.com/sokolaidev/maf-extensions/issues/1606)) ([5b70026](https://github.com/sokolaidev/maf-extensions/commit/5b70026dda8fb1ca68ce49f6a7a79caf5f3d5b04))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.3.0...maf-sandbox-docker-sbx-v0.4.0) (2026-09-30)


### Features

* **docker-sbx:** serve Egress.ALLOWLIST with host, method and path rules, refusing hosts whose global rules or secrets would widen it ([#1583](https://github.com/sokolaidev/maf-extensions/issues/1583)) ([ce73a18](https://github.com/sokolaidev/maf-extensions/commit/ce73a18c372715553ad297be7b722ddb0093a7cf))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.2.1...maf-sandbox-docker-sbx-v0.3.0) (2026-09-29)


### Features

* **docker-sbx:** serve exec_bounded, so Deep Agents and other callers can set their own output budget ([#1576](https://github.com/sokolaidev/maf-extensions/issues/1576)) ([0090ebe](https://github.com/sokolaidev/maf-extensions/commit/0090ebeb85b89c24d38a378193c5ac35002198e0))


### Fixes

* **docker-sbx:** name the unshare options an image needs instead of a util-linux version ([#1574](https://github.com/sokolaidev/maf-extensions/issues/1574)) ([ddeceb2](https://github.com/sokolaidev/maf-extensions/commit/ddeceb24267625dd15e929a9d4828d909cf0b7f1))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.2.0...maf-sandbox-docker-sbx-v0.2.1) (2026-09-28)


### Fixes

* require maf-sandbox 0.45.0 in the dependents, and admit the 0.45 line ([#1546](https://github.com/sokolaidev/maf-extensions/issues/1546)) ([2c01a73](https://github.com/sokolaidev/maf-extensions/commit/2c01a738790c4ea2ba452c250715226916c20d20))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-docker-sbx-v0.1.0...maf-sandbox-docker-sbx-v0.2.0) (2026-09-27)


### ⚠ BREAKING CHANGES

* **docker-sbx:** serve any template by binding the workspace in each command's own namespace, which leaves commands without sudo ([#1533](https://github.com/sokolaidev/maf-extensions/issues/1533))

### Fixes

* **docker-sbx:** serve any template by binding the workspace in each command's own namespace, which leaves commands without sudo ([#1533](https://github.com/sokolaidev/maf-extensions/issues/1533)) ([92daa48](https://github.com/sokolaidev/maf-extensions/commit/92daa4889e2481f8098b9997870efc763ad1dc07))


### Documentation

* **docker-sbx:** record the Linux live results, where the guest makes links the file plane never follows ([#1505](https://github.com/sokolaidev/maf-extensions/issues/1505)) ([b45566b](https://github.com/sokolaidev/maf-extensions/commit/b45566ba6134a4d7cb3215ce7f28222b1fd26ece))

## 0.1.0 (2026-09-26)


### Features

* **docker-sbx:** add a Docker Sandboxes backend that serves a local microVM at the MICROVM floor ([#1447](https://github.com/sokolaidev/maf-extensions/issues/1447)) ([3afefb2](https://github.com/sokolaidev/maf-extensions/commit/3afefb25912db6aa2c23832d97a15031d9ab4b89))


### Fixes

* require maf-sandbox 0.44.0 in the dependents, and admit the 0.44 line ([#1489](https://github.com/sokolaidev/maf-extensions/issues/1489)) ([90e5f6f](https://github.com/sokolaidev/maf-extensions/commit/90e5f6f04f87b18799536401eaab8b2e676be2af))
