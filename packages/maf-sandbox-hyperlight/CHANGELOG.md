# Changelog

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.5.1...maf-sandbox-hyperlight-v0.6.0) (2026-09-26)


### ⚠ BREAKING CHANGES

* **hyperlight:** stop publishing a pod's scope, thread and agent in its spec ([#1470](https://github.com/sokolaidev/maf-extensions/issues/1470))

### Features

* **hyperlight:** enforce HTTP method rules through the runtime's own allowlist ([#1448](https://github.com/sokolaidev/maf-extensions/issues/1448)) ([bacc886](https://github.com/sokolaidev/maf-extensions/commit/bacc8864d6d4250de6940c59ba428265069c6fa4))
* **hyperlight:** refuse AKS nodes that fail pod-mode requirements before the application starts ([#1486](https://github.com/sokolaidev/maf-extensions/issues/1486)) ([02c0dce](https://github.com/sokolaidev/maf-extensions/commit/02c0dcea8efbf7a9dc06452e22199d18934cc284))
* **hyperlight:** report why a Kubernetes pod retired, after its cleanup ([#1487](https://github.com/sokolaidev/maf-extensions/issues/1487)) ([c858d48](https://github.com/sokolaidev/maf-extensions/commit/c858d48d342fec41fcccc40ab3b7000b53b0f2ed))


### Fixes

* **hyperlight:** keep the pod's other processes out of PID 1's controller stdin ([#1491](https://github.com/sokolaidev/maf-extensions/issues/1491)) ([d75a448](https://github.com/sokolaidev/maf-extensions/commit/d75a4489c01a799c36eff01bf5238fffacc959c2))
* **hyperlight:** stop publishing a pod's scope, thread and agent in its spec ([#1470](https://github.com/sokolaidev/maf-extensions/issues/1470)) ([05c2c8b](https://github.com/sokolaidev/maf-extensions/commit/05c2c8ba12823cb0dcdf2927a50d1b03feb1e19c))
* require maf-sandbox 0.44.0 in the dependents, and admit the 0.44 line ([#1489](https://github.com/sokolaidev/maf-extensions/issues/1489)) ([90e5f6f](https://github.com/sokolaidev/maf-extensions/commit/90e5f6f04f87b18799536401eaab8b2e676be2af))

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.5.0...maf-sandbox-hyperlight-v0.5.1) (2026-09-24)


### Fixes

* **hyperlight:** report API recovery failures as pending cleanup ([#1411](https://github.com/sokolaidev/maf-extensions/issues/1411)) ([0e19592](https://github.com/sokolaidev/maf-extensions/commit/0e195926ea003053f64bea570074033149108d1d))
* require maf-sandbox 0.43.0 in the dependents, and admit the 0.43 line ([#1431](https://github.com/sokolaidev/maf-extensions/issues/1431)) ([3fb4272](https://github.com/sokolaidev/maf-extensions/commit/3fb4272bc047b35de34fd05e523d15ee7fb70be3))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.4.0...maf-sandbox-hyperlight-v0.5.0) (2026-09-23)


### Features

* **hyperlight:** run one sandbox ownership scope per Kubernetes pod ([#1406](https://github.com/sokolaidev/maf-extensions/issues/1406)) ([9784bfe](https://github.com/sokolaidev/maf-extensions/commit/9784bfef7a3dddd4484339783c45873eacaa7849))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.3.0...maf-sandbox-hyperlight-v0.4.0) (2026-09-22)


### Features

* **hyperlight:** enumerate flat output files through trusted host storage ([#1397](https://github.com/sokolaidev/maf-extensions/issues/1397)) ([5062ca4](https://github.com/sokolaidev/maf-extensions/commit/5062ca46cc90a394d02740f905a9ad4feed3aad7))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.2.0...maf-sandbox-hyperlight-v0.3.0) (2026-09-21)


### Features

* **core,hyperlight,codeact,tui:** collect isolated Hyperlight output files ([#1344](https://github.com/sokolaidev/maf-extensions/issues/1344)) ([8a2ea67](https://github.com/sokolaidev/maf-extensions/commit/8a2ea67c2da513c1e5dd4efcf3b6d62efef99bfb))


### Documentation

* clarify package setup, usage and limits ([#1370](https://github.com/sokolaidev/maf-extensions/issues/1370)) ([b0dd379](https://github.com/sokolaidev/maf-extensions/commit/b0dd3791cb18fab0b7c565e327ee872b4620c6f7))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-hyperlight-v0.1.0...maf-sandbox-hyperlight-v0.2.0) (2026-09-19)


### ⚠ BREAKING CHANGES

* adopt agent-framework-core 1.19, and re-pair every package on maf-sandbox 0.41 ([#1316](https://github.com/sokolaidev/maf-extensions/issues/1316))

### Features

* adopt agent-framework-core 1.19, and re-pair every package on maf-sandbox 0.41 ([#1316](https://github.com/sokolaidev/maf-extensions/issues/1316)) ([a1a2dec](https://github.com/sokolaidev/maf-extensions/commit/a1a2dec8ff5bb936d4c044b666b74dd5dc4c9b1b))

## 0.1.0 (2026-09-14)


### Features

* **hyperlight:** run Python in Hyperlight microVMs on Windows ([#1223](https://github.com/sokolaidev/maf-extensions/issues/1223)) ([82c7521](https://github.com/sokolaidev/maf-extensions/commit/82c75212932a1edf1e91547d3478e1f6d8ffce28))
* **hyperlight:** run Python microVMs on Linux KVM and WSL2 ([#1231](https://github.com/sokolaidev/maf-extensions/issues/1231)) ([91a5488](https://github.com/sokolaidev/maf-extensions/commit/91a5488fc927f8927ec32fed31265577bdfba570))


### Documentation

* remove issue references from package descriptions ([#1257](https://github.com/sokolaidev/maf-extensions/issues/1257)) ([1f2d5a5](https://github.com/sokolaidev/maf-extensions/commit/1f2d5a59b2d3c41e2f07c492905b88ec9f493d33))

## Changelog

Release notes are generated by release-please.
