# Changelog

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-otel-v0.3.0...maf-sandbox-otel-v0.4.0) (2026-09-10)


### Features

* accept method-scoped egress policies and refuse unsupported backends ([#1063](https://github.com/sokolaidev/maf-extensions/issues/1063)) ([39fd53b](https://github.com/sokolaidev/maf-extensions/commit/39fd53b457a301d59a646d815e717b8e61b6d698))


### Fixes

* require maf-sandbox 0.37.0 in the dependents, and admit the 0.37 line ([#1075](https://github.com/sokolaidev/maf-extensions/issues/1075)) ([8eb88fd](https://github.com/sokolaidev/maf-extensions/commit/8eb88fdfc706da4fe820a8388698dc37cb0b05f0))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-otel-v0.2.0...maf-sandbox-otel-v0.3.0) (2026-09-08)


### Features

* **otel:** a record names the call it came from, and an acquire names the tools that were callable ([#988](https://github.com/sokolaidev/maf-extensions/issues/988)) ([2e185d5](https://github.com/sokolaidev/maf-extensions/commit/2e185d5a6d7c9f3f98b8380f7790944c2f7a95db))


### Fixes

* require maf-sandbox 0.36.0 in the dependents, and admit the 0.36 line ([#1018](https://github.com/sokolaidev/maf-extensions/issues/1018)) ([d58e1ac](https://github.com/sokolaidev/maf-extensions/commit/d58e1ac02d0372e356cf00dbce5fe59c38b35324))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-otel-v0.1.1...maf-sandbox-otel-v0.2.0) (2026-09-07)


### Features

* a backend reports what its egress enforcement decided, so a record separates what a sandbox was allowed to reach from what it did ([#963](https://github.com/sokolaidev/maf-extensions/issues/963)) ([20b5f50](https://github.com/sokolaidev/maf-extensions/commit/20b5f5044076085525c2bb2a66bfa893dc1cbe55))
* **otel:** an acquire record says under whose authority the run could act, and whether its host-tool surface is fully stamped ([#956](https://github.com/sokolaidev/maf-extensions/issues/956)) ([5ace9ec](https://github.com/sokolaidev/maf-extensions/commit/5ace9ec55621ecf78012c11f1ed1417bda1379f5))
* the scope purge is recorded, so the cleanup a thread deletion runs is no longer the one disposal nobody can see ([#947](https://github.com/sokolaidev/maf-extensions/issues/947)) ([580790d](https://github.com/sokolaidev/maf-extensions/commit/580790d36ad3969e5333bbf31ef93f46c6889b3e))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-otel-v0.1.0...maf-sandbox-otel-v0.1.1) (2026-09-06)


### Documentation

* **otel:** the package page carries the badge row and the pre-1.0 banner, and no link that only resolves in the repository ([#938](https://github.com/sokolaidev/maf-extensions/issues/938)) ([202cd71](https://github.com/sokolaidev/maf-extensions/commit/202cd71ea4c81e030b67d32b9f3718fba2bd5000))

## 0.1.0 (2026-09-05)


### Features

* **otel:** a sandbox observer that records egress posture, host-tool calls, file crossings and disposal to OpenTelemetry ([#907](https://github.com/sokolaidev/maf-extensions/issues/907)) ([0c0d4dc](https://github.com/sokolaidev/maf-extensions/commit/0c0d4dcece5b54e04b216d7cb5b1b8ac42552943))


### Fixes

* admit maf-sandbox 0.35 in the dependents' range, and require 0.34 in the samples ([#930](https://github.com/sokolaidev/maf-extensions/issues/930)) ([4293f12](https://github.com/sokolaidev/maf-extensions/commit/4293f1224533f5074eb204321af6824b03268691))
