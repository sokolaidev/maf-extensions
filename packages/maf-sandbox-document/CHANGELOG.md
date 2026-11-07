# Changelog

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.2.2...maf-sandbox-document-v0.3.0) (2026-11-07)


### Features

* **document:** export Markdown reports as PDFs with Mermaid and SVG assets ([#2528](https://github.com/sokolaidev/maf-extensions/issues/2528)) ([97bf3a7](https://github.com/sokolaidev/maf-extensions/commit/97bf3a706cd5bcb733ba6302f3d05fdf80537b35))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.2.1...maf-sandbox-document-v0.2.2) (2026-11-05)


### Fixes

* preserve provenance and provider lifetimes on MAF 1.24 ([#2344](https://github.com/sokolaidev/maf-extensions/issues/2344)) ([4ff8a3d](https://github.com/sokolaidev/maf-extensions/commit/4ff8a3d310f7ec26a9bcd7858bdec46da03f5f49))
* require maf-sandbox 0.53.0 in the dependents, and admit the 0.53 line ([#2440](https://github.com/sokolaidev/maf-extensions/issues/2440)) ([333e291](https://github.com/sokolaidev/maf-extensions/commit/333e29138ca2f87782f488b89d8664a2a764a341))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.2.0...maf-sandbox-document-v0.2.1) (2026-10-30)


### Documentation

* **document:** document installation from PyPI ([#2334](https://github.com/sokolaidev/maf-extensions/issues/2334)) ([1aee8c5](https://github.com/sokolaidev/maf-extensions/commit/1aee8c5244e88cf6701ba3d332f07033b199d2b3))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.1.0...maf-sandbox-document-v0.2.0) (2026-10-28)


### Features

* adopt MAF 1.23 and MCP 2 across the suite ([#2140](https://github.com/sokolaidev/maf-extensions/issues/2140)) ([9ea47ea](https://github.com/sokolaidev/maf-extensions/commit/9ea47eac75a81ef01361a99bc405465fb54733ac))

## 0.1.0 (2026-10-27)

**Not published to PyPI.** The [publish run](https://github.com/sokolaidev/maf-extensions/actions/runs/40524663264) failed dependency validation: this release requires `agent-framework-core>=1.22.0,<1.23`, while its required `maf-sandbox>=0.52.0,<0.53` resolves to core 0.52.0, which requires `agent-framework-core>=1.23.0,<1.24`. The [GitHub release and tag](https://github.com/sokolaidev/maf-extensions/releases/tag/maf-sandbox-document-v0.1.0) are immutable, so 0.1.0 cannot be repointed to the corrected source. [#2140](https://github.com/sokolaidev/maf-extensions/pull/2140) aligns the framework dependency; [#2259](https://github.com/sokolaidev/maf-extensions/pull/2259) prepares 0.2.0 as the replacement release, whose PyPI publication remains pending.


### Features

* add guarded document extraction and binary file-store reads ([#2215](https://github.com/sokolaidev/maf-extensions/issues/2215)) ([6ee1c50](https://github.com/sokolaidev/maf-extensions/commit/6ee1c500105e22a510dbd2dde46c23ed46686afa))
