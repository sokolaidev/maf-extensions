# Changelog

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
