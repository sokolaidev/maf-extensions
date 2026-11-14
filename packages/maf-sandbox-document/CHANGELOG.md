# Changelog

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.4.1...maf-sandbox-document-v0.5.0) (2026-11-14)


### Features

* **sandbox:** expose fixed result sentences on attached tools ([#2683](https://github.com/sokolaidev/maf-extensions/issues/2683)) ([822f8bf](https://github.com/sokolaidev/maf-extensions/commit/822f8bf4efdd3c7fbcb050db8a1b82c57fd4258a))


### Fixes

* preserve sandbox diagnostics with FIDES quarantine disabled ([#2668](https://github.com/sokolaidev/maf-extensions/issues/2668)) ([ed3cede](https://github.com/sokolaidev/maf-extensions/commit/ed3ceded1c2de309ed4d707f396f90906cec20a7))

## [0.4.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.4.0...maf-sandbox-document-v0.4.1) (2026-11-10)


### Miscellaneous

* prepare document 0.4.1 and require release tags for PyPI ([de14326](https://github.com/sokolaidev/maf-extensions/commit/de14326ba892b7e0ba070d24af68b57f5de034f1))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.3.1...maf-sandbox-document-v0.4.0) (2026-11-10)

> **Attestation correction.** This version reached PyPI through a [workflow dispatched from `main`](https://github.com/sokolaidev/maf-extensions/actions/runs/42793253517), so its wheel and sdist attestations identify `refs/heads/main` rather than `refs/tags/maf-sandbox-document-v0.4.0`. Consumers requiring the exact release-tag identity reject these artifacts. The 0.4.1 replacement was [published](https://github.com/sokolaidev/maf-extensions/actions/runs/42898616294) from `refs/tags/maf-sandbox-document-v0.4.1` at commit `31265eeefc71732d381557e8409332be3ddc33b2`. Both published artifacts passed cryptographic attestation verification against that exact tag, commit and publish-workflow identity; both rejected `refs/heads/main` as the expected source ref. [#2645](https://github.com/sokolaidev/maf-extensions/pull/2645) records the verification results and artifact digests, with links to the original provenance on PyPI. See [#2639](https://github.com/sokolaidev/maf-extensions/issues/2639).


### Features

* **document:** expose SVG and asset-name preflight checks ([#2609](https://github.com/sokolaidev/maf-extensions/issues/2609)) ([a0ecd56](https://github.com/sokolaidev/maf-extensions/commit/a0ecd56c97ee3f0e61bd31709749624f36272aeb))
* **document:** render unsupplied images as optional placeholders ([#2611](https://github.com/sokolaidev/maf-extensions/issues/2611)) ([f185bff](https://github.com/sokolaidev/maf-extensions/commit/f185bff7650a1f3463de17320e567c8cf433c641))

## [0.3.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.3.0...maf-sandbox-document-v0.3.1) (2026-11-08)


### Miscellaneous

* **document:** record the unpublished 0.3.0 release ([#2581](https://github.com/sokolaidev/maf-extensions/issues/2581)) ([9beaafd](https://github.com/sokolaidev/maf-extensions/commit/9beaafda3df7b099679d763d10af704986a62470))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-document-v0.2.2...maf-sandbox-document-v0.3.0) (2026-11-07)

> **Publication correction.** This version has an immutable GitHub Release and tag but did not reach PyPI. [Publication attempt 3](https://github.com/sokolaidev/maf-extensions/actions/runs/42427269701/job/128860073339) stopped before building because the published `document-export` image lacked consumer qualification matching this release's frozen inputs. [Qualification on newer `main`](https://github.com/sokolaidev/maf-extensions/actions/runs/42522222327) passed, but its inputs differ from this tag. The replacement is planned as 0.3.1 through release-please; this note does not establish that the replacement has published.


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
