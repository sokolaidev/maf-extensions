# Changelog

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.1.0...maf-sandbox-document-v0.2.0) (2026-10-28)


### Features

* adopt MAF 1.23 and MCP 2 across the suite ([#2140](https://github.com/sokolaidev/maf-extensions/issues/2140)) ([9ea47ea](https://github.com/sokolaidev/maf-extensions/commit/9ea47eac75a81ef01361a99bc405465fb54733ac))

## 0.1.0 (2026-10-27)

**Not published to PyPI.** The [publish run](https://github.com/sokolaidev/maf-extensions/actions/runs/40524663264) failed dependency validation: this release requires `agent-framework-core>=1.22.0,<1.23`, while its required `maf-sandbox>=0.52.0,<0.53` resolves to core 0.52.0, which requires `agent-framework-core>=1.23.0,<1.24`. The [GitHub release and tag](https://github.com/sokolaidev/maf-extensions/releases/tag/maf-sandbox-document-v0.1.0) are immutable, so 0.1.0 cannot be repointed to the corrected source. [#2140](https://github.com/sokolaidev/maf-extensions/pull/2140) aligns the framework dependency; [#2259](https://github.com/sokolaidev/maf-extensions/pull/2259) prepares 0.2.0 as the replacement release, whose PyPI publication remains pending.


### Features

* add guarded document extraction and binary file-store reads ([#2215](https://github.com/sokolaidev/maf-extensions/issues/2215)) ([6ee1c50](https://github.com/sokolaidev/maf-extensions/commit/6ee1c500105e22a510dbd2dde46c23ed46686afa))
