# Changelog

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.2.0...maf-compaction-v0.3.0) (2026-10-15)


### Features

* **compaction:** preserve and budget media with optional descriptions ([#1942](https://github.com/sokolaidev/maf-extensions/issues/1942)) ([1403ac0](https://github.com/sokolaidev/maf-extensions/commit/1403ac07c7c4b2be064ffc5ad0edeef2a215d908))
* support MAF 1.21 across sandbox and compaction packages ([#1900](https://github.com/sokolaidev/maf-extensions/issues/1900)) ([ff70c0d](https://github.com/sokolaidev/maf-extensions/commit/ff70c0da983032c8aa6efcdeaa4f586183ffddec))


### Fixes

* **compaction:** bound recall retries and prevent oversized prompts ([#1880](https://github.com/sokolaidev/maf-extensions/issues/1880)) ([16a3c53](https://github.com/sokolaidev/maf-extensions/commit/16a3c53bcea7f1f4f1ba012b9d9ffe4d4abff436))
* **compaction:** bound summarizer input to a configurable budget ([#1903](https://github.com/sokolaidev/maf-extensions/issues/1903)) ([53f93ff](https://github.com/sokolaidev/maf-extensions/commit/53f93ffb5cad2ea1297f30483816c2afe014e900))
* **compaction:** send shortened tool results to model providers ([#1940](https://github.com/sokolaidev/maf-extensions/issues/1940)) ([7a2c30f](https://github.com/sokolaidev/maf-extensions/commit/7a2c30f75ae50aaa233581504b7704aefbd039d2))
* **compaction:** stop forcing recall when providers ignore or reject the pin ([#1941](https://github.com/sokolaidev/maf-extensions/issues/1941)) ([50c38b3](https://github.com/sokolaidev/maf-extensions/commit/50c38b3347ca936d7c3905f528f4810137a32cda))
* release recorded table rows using the configured record target ([#1893](https://github.com/sokolaidev/maf-extensions/issues/1893)) ([b5e756d](https://github.com/sokolaidev/maf-extensions/commit/b5e756d6a8c80cd7095a3f7878e708611c04b0c5))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.1.0...maf-compaction-v0.2.0) (2026-10-11)


### Features

* **compaction:** persist per-session state for shared-agent compaction ([#1830](https://github.com/sokolaidev/maf-extensions/issues/1830)) ([23eae15](https://github.com/sokolaidev/maf-extensions/commit/23eae15d9ff2bd6d29d679aa4d8011cd2c2c8820))


### Documentation

* **compaction:** condense strategy docstrings to their contracts ([#1810](https://github.com/sokolaidev/maf-extensions/issues/1810)) ([1a87cbd](https://github.com/sokolaidev/maf-extensions/commit/1a87cbd3796ab6e337274f8d45b11858eb8b0f06))

## 0.1.0 (2026-10-06)


### Features

* add maf-compaction, prompt-cache-aware compaction strategies for Microsoft Agent Framework ([#1759](https://github.com/sokolaidev/maf-extensions/issues/1759)) ([ba6238c](https://github.com/sokolaidev/maf-extensions/commit/ba6238c01f33b4ad7806998c250c821eacd6d340))
