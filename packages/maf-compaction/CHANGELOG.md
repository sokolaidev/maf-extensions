# Changelog

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.9.0...maf-compaction-v0.10.0) (2026-10-27)


### Features

* adopt MAF 1.23 and MCP 2 across the suite ([#2140](https://github.com/sokolaidev/maf-extensions/issues/2140)) ([9ea47ea](https://github.com/sokolaidev/maf-extensions/commit/9ea47eac75a81ef01361a99bc405465fb54733ac))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.8.0...maf-compaction-v0.9.0) (2026-10-26)


### Features

* **compaction:** add fenced saves, session branching and persistence hooks ([#2203](https://github.com/sokolaidev/maf-extensions/issues/2203)) ([99284ae](https://github.com/sokolaidev/maf-extensions/commit/99284ae1281387aecfd875ece1bd3f8f32dbae66))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.7.1...maf-compaction-v0.8.0) (2026-10-25)


### Features

* **compaction:** load and save sessions around complete agent requests ([#2169](https://github.com/sokolaidev/maf-extensions/issues/2169)) ([923e0ea](https://github.com/sokolaidev/maf-extensions/commit/923e0ea3b98e0cbe93451346a1b1358fe5ce6c2c))


### Fixes

* **compaction:** replay user summaries when hosts resend original history ([#2157](https://github.com/sokolaidev/maf-extensions/issues/2157)) ([a4fe368](https://github.com/sokolaidev/maf-extensions/commit/a4fe3686102069270ba67f1c12db13c6011dc5f8))

## [0.7.1](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.7.0...maf-compaction-v0.7.1) (2026-10-24)


### Fixes

* **compaction:** keep completed requests visibly answered after shedding ([#2149](https://github.com/sokolaidev/maf-extensions/issues/2149)) ([ab0d4af](https://github.com/sokolaidev/maf-extensions/commit/ab0d4af1738495d863e196336ceb7e147ecc4935))
* **compaction:** retry truncated recall and batch no-record overflow ([#2147](https://github.com/sokolaidev/maf-extensions/issues/2147)) ([4d21c31](https://github.com/sokolaidev/maf-extensions/commit/4d21c314ec36b4d8ffbeb7c2bc9d830529b8d8c1))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.7...maf-compaction-v0.7.0) (2026-10-24)


### Features

* **compaction:** allow per-call instructions for pinned recall records ([#2121](https://github.com/sokolaidev/maf-extensions/issues/2121)) ([da0711c](https://github.com/sokolaidev/maf-extensions/commit/da0711c63570c0801bd2adc3a28371a6f293c5eb))
* **compaction:** preserve_prompt_cache recall ask that keeps tools and tool_choice ([#2117](https://github.com/sokolaidev/maf-extensions/issues/2117)) ([6cfba96](https://github.com/sokolaidev/maf-extensions/commit/6cfba96330d162d9774ad94d18f3e0376d25b379))


### Fixes

* **compaction:** leave headroom after overflow shedding ([#2124](https://github.com/sokolaidev/maf-extensions/issues/2124)) ([0969fdd](https://github.com/sokolaidev/maf-extensions/commit/0969fdda89aeb1f51c97eb19ae5d96c23b24b58d))
* **compaction:** reject refused recall records and limit requests to tool results ([#2123](https://github.com/sokolaidev/maf-extensions/issues/2123)) ([733dea1](https://github.com/sokolaidev/maf-extensions/commit/733dea1e89c98b086f8d9bbd3546f1a68b573fa9))

## [0.6.7](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.6...maf-compaction-v0.6.7) (2026-10-23)


### Fixes

* **compaction:** disable value-free recall pins when re-forcing is off ([#2109](https://github.com/sokolaidev/maf-extensions/issues/2109)) ([809069c](https://github.com/sokolaidev/maf-extensions/commit/809069ca7b28f5c64df51bdf63d198ea902e4321))

## [0.6.6](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.5...maf-compaction-v0.6.6) (2026-10-23)


### Fixes

* **compaction:** retain recall targets for backoff after overflow shedding ([#2103](https://github.com/sokolaidev/maf-extensions/issues/2103)) ([c6c9e6c](https://github.com/sokolaidev/maf-extensions/commit/c6c9e6c81d36369465e99f285c4cab7d8c368d39))

## [0.6.5](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.4...maf-compaction-v0.6.5) (2026-10-21)


### Fixes

* **compaction:** fall back to prompt recall for wrapped tool-choice errors ([#2091](https://github.com/sokolaidev/maf-extensions/issues/2091)) ([2c10f2c](https://github.com/sokolaidev/maf-extensions/commit/2c10f2c61619658d7e284afc89e3e5206cafb9dd))

## [0.6.4](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.3...maf-compaction-v0.6.4) (2026-10-21)


### Fixes

* **compaction:** strip stale recall prompts and accept structured records ([#2084](https://github.com/sokolaidev/maf-extensions/issues/2084)) ([030e9fa](https://github.com/sokolaidev/maf-extensions/commit/030e9fa969d6655a54b838252f436669cda355ef))

## [0.6.3](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.2...maf-compaction-v0.6.3) (2026-10-20)


### Fixes

* **compaction:** put prompted recall last and advertise only the recall tool ([#2072](https://github.com/sokolaidev/maf-extensions/issues/2072)) ([fedb765](https://github.com/sokolaidev/maf-extensions/commit/fedb765ac00be7a40438b2aa6c1f5d0715019dc6))

## [0.6.2](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.1...maf-compaction-v0.6.2) (2026-10-20)


### Fixes

* **compaction:** request recall by prompt when tool choice is unsupported ([#2068](https://github.com/sokolaidev/maf-extensions/issues/2068)) ([e2ce91a](https://github.com/sokolaidev/maf-extensions/commit/e2ce91a00f32008a2d7f5fcd4bbe0e207351f13c))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.6.0...maf-compaction-v0.6.1) (2026-10-19)


### Fixes

* **compaction:** preserve replies, honor summary options, and back off value-free recall ([#2042](https://github.com/sokolaidev/maf-extensions/issues/2042)) ([8fd2b51](https://github.com/sokolaidev/maf-extensions/commit/8fd2b51b50291393f68f4431697c99018d00a5ad))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.5.0...maf-compaction-v0.6.0) (2026-10-18)


### Features

* support MAF 1.22 and distinguish recall call occurrences ([#1963](https://github.com/sokolaidev/maf-extensions/issues/1963)) ([7c0194c](https://github.com/sokolaidev/maf-extensions/commit/7c0194c293bef15753b952e10318dbb7e122181b))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.4.0...maf-compaction-v0.5.0) (2026-10-18)


### Features

* **compaction:** allow host-controlled shortening of labelled and mixed tool results ([#2001](https://github.com/sokolaidev/maf-extensions/issues/2001)) ([c61b2cd](https://github.com/sokolaidev/maf-extensions/commit/c61b2cdc6f7e75258f3b2cbf20fef08fe468ac33))
* **compaction:** budget request overhead, chunk summaries and retry malformed recall ([#2008](https://github.com/sokolaidev/maf-extensions/issues/2008)) ([240e0dd](https://github.com/sokolaidev/maf-extensions/commit/240e0ddbd8e0b3517762a9484d8d8a27d948a991))


### Fixes

* **compaction:** persist summary cooldowns across session restores ([#2012](https://github.com/sokolaidev/maf-extensions/issues/2012)) ([7beef82](https://github.com/sokolaidev/maf-extensions/commit/7beef826d2c8e9567ccb000b5cf204f02bad4120))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-compaction-v0.3.0...maf-compaction-v0.4.0) (2026-10-16)


### Features

* **compaction:** add user-summary timeouts and failure backoff ([#1984](https://github.com/sokolaidev/maf-extensions/issues/1984)) ([087fc77](https://github.com/sokolaidev/maf-extensions/commit/087fc7798afbb274cb42d962fc3dd4a2bcae7443))

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
