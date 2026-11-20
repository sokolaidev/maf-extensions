# Changelog

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.6.1...maf-cachebench-v0.7.0) (2026-11-20)


### Features

* **cachebench:** benchmark background record merges and reduction controls ([#2776](https://github.com/sokolaidev/maf-extensions/issues/2776)) ([07e22c6](https://github.com/sokolaidev/maf-extensions/commit/07e22c6067e7ff07690971509b504a4d91768ec7))
* **cachebench:** measure whether record merges preserve the roles of values ([#2809](https://github.com/sokolaidev/maf-extensions/issues/2809)) ([73bc634](https://github.com/sokolaidev/maf-extensions/commit/73bc63464f79a9a0218ebfba0c72dd5a76233338))
* **cachebench:** measure which record merges lose the next deployment target ([#2800](https://github.com/sokolaidev/maf-extensions/issues/2800)) ([418305a](https://github.com/sokolaidev/maf-extensions/commit/418305a397f9c1ba9cbc9d923722699c647ac407))
* **compaction:** merge Responses recall records and qualify them in Cachebench ([#2799](https://github.com/sokolaidev/maf-extensions/issues/2799)) ([9af61a9](https://github.com/sokolaidev/maf-extensions/commit/9af61a9818e2d5844bb9487d62b0ec99507d069c))


### Fixes

* require maf-compaction 0.13.1 in the dependents, and admit the 0.13 line ([#2710](https://github.com/sokolaidev/maf-extensions/issues/2710)) ([c1ecf40](https://github.com/sokolaidev/maf-extensions/commit/c1ecf40838cda20cc77dc55d18c1c22b4ce7b417))
* require maf-compaction 0.14.0 in the dependents, and admit the 0.14 line ([#2789](https://github.com/sokolaidev/maf-extensions/issues/2789)) ([36171fa](https://github.com/sokolaidev/maf-extensions/commit/36171fa5661b8eeda1c7d92ef81bcc8f42cd847f))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.6.0...maf-cachebench-v0.6.1) (2026-11-10)


### Fixes

* **cachebench:** support the maf-compaction 0.12 release line ([#2612](https://github.com/sokolaidev/maf-extensions/issues/2612)) ([d8e68c3](https://github.com/sokolaidev/maf-extensions/commit/d8e68c34a64d2882626fa1434d4ad3c042c1a7aa))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.5.1...maf-cachebench-v0.6.0) (2026-11-08)


### Features

* **cachebench:** benchmark persistence extensions and host hooks ([#2521](https://github.com/sokolaidev/maf-extensions/issues/2521)) ([60e54f8](https://github.com/sokolaidev/maf-extensions/commit/60e54f8b928fcb17a5a6130d3f1dd8a2b196c710))

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.5.0...maf-cachebench-v0.5.1) (2026-11-04)


### Fixes

* **maf-compaction:** keep a record merge or rewrite only if it also keeps the records' values ([#2456](https://github.com/sokolaidev/maf-extensions/issues/2456)) ([450d646](https://github.com/sokolaidev/maf-extensions/commit/450d6466ef04df06966bda3bc5c0e835121d3a84))
* preserve provenance and provider lifetimes on MAF 1.24 ([#2344](https://github.com/sokolaidev/maf-extensions/issues/2344)) ([4ff8a3d](https://github.com/sokolaidev/maf-extensions/commit/4ff8a3d310f7ec26a9bcd7858bdec46da03f5f49))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.4.1...maf-cachebench-v0.5.0) (2026-10-28)


### Features

* adopt MAF 1.23 and MCP 2 across the suite ([#2140](https://github.com/sokolaidev/maf-extensions/issues/2140)) ([9ea47ea](https://github.com/sokolaidev/maf-extensions/commit/9ea47eac75a81ef01361a99bc405465fb54733ac))

## [0.4.1](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.4.0...maf-cachebench-v0.4.1) (2026-10-27)


### Fixes

* **deps-dev:** bump agent-framework-ollama from 1.0.0b261015 to 1.0.0b261022 ([#2193](https://github.com/sokolaidev/maf-extensions/issues/2193)) ([172fd85](https://github.com/sokolaidev/maf-extensions/commit/172fd85f82d5c806ad6dc56357a1924b54e5b1a5))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.3.1...maf-cachebench-v0.4.0) (2026-10-26)


### Features

* **cachebench:** benchmark compaction recovery, replay and streaming ([#2179](https://github.com/sokolaidev/maf-extensions/issues/2179)) ([afbc4ba](https://github.com/sokolaidev/maf-extensions/commit/afbc4bac420f5447009d01caea9cd01db260e2e7))

## [0.3.1](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.3.0...maf-cachebench-v0.3.1) (2026-10-26)


### Fixes

* **cachebench:** admit compaction 0.8 and verify live integrations ([#2184](https://github.com/sokolaidev/maf-extensions/issues/2184)) ([5312cc7](https://github.com/sokolaidev/maf-extensions/commit/5312cc7164eb256c8b65dbeac7e32dbc79b3782b))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.2.0...maf-cachebench-v0.3.0) (2026-10-19)


### Features

* support MAF 1.22 and distinguish recall call occurrences ([#1963](https://github.com/sokolaidev/maf-extensions/issues/1963)) ([7c0194c](https://github.com/sokolaidev/maf-extensions/commit/7c0194c293bef15753b952e10318dbb7e122181b))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-cachebench-v0.1.0...maf-cachebench-v0.2.0) (2026-10-14)


### Features

* support MAF 1.21 across sandbox and compaction packages ([#1900](https://github.com/sokolaidev/maf-extensions/issues/1900)) ([ff70c0d](https://github.com/sokolaidev/maf-extensions/commit/ff70c0da983032c8aa6efcdeaa4f586183ffddec))


### Fixes

* release recorded table rows using the configured record target ([#1893](https://github.com/sokolaidev/maf-extensions/issues/1893)) ([b5e756d](https://github.com/sokolaidev/maf-extensions/commit/b5e756d6a8c80cd7095a3f7878e708611c04b0c5))

## 0.1.0 (2026-10-07)


### Features

* add maf-cachebench, the benchmark the compaction strategies were measured with ([#1760](https://github.com/sokolaidev/maf-extensions/issues/1760)) ([adf2bca](https://github.com/sokolaidev/maf-extensions/commit/adf2bca576efd65a57b5adb61422db3d04248149))
