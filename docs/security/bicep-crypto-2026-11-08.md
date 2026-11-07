# Bicep AVM cryptography rebuild

The local Linux/amd64 rebuild for [#2522](https://github.com/sokolaidev/maf-extensions/issues/2522) contains `System.Security.Cryptography.Pkcs` and `System.Security.Cryptography.Xml` at **10.0.13** in the actual compiler bundle. Both were 10.0.10 in the issue's published 0.2.1 observation. [#2523](https://github.com/sokolaidev/maf-extensions/pull/2523) already upgraded the pinned upstream compiler to Bicep 0.49.2; this qualification verifies that bundle and the prepared AVM image. No additional compiler or Python package version change is needed.

## Candidate checks

New candidate inventory checks require both cryptography packages at a stable numeric version of at least 10.0.13, located in `/usr/local/bin/bicep`. They reject missing inventory, contradictory versions, prerelease versions and packages found only in another executable. The checks cover `bicep`, `bicep-avm` and `bicep-docker-sbx`, including upstream and source routes, before release assessment. Source builds additionally retain their exact receipt/package checks. Historical monitoring continues to assess the original immutable images and does not apply this candidate floor retroactively.

This is a build requirement for these two dependencies, not a replacement for vulnerability scanning. Future advisories can affect versions that satisfy the floor. The ordinary Critical publication threshold, High monitoring policy, runtime qualification and protected publication gates still apply.

## Retained local evidence

The [evidence index](evidence/bicep-crypto-2026-11-08/evidence.json) records source `ddbe68951cf372a0262b23e5e969e4fe48a44b93`, file hashes and the local identities. Image inputs were unchanged by the candidate-check implementation. Builds used the ordinary upstream and prepared-image recipes. The compiler binary SHA-256 is `0ab9de90c2493f206f8281bae74f9ee2d5ed3605548dab1adf41fef185e1f7c5` and its reported version is `0.49.2 (b34271ac51)`.

| Identity | Digest |
|---|---|
| Docker Desktop local image index used by runtime checks | `sha256:64cff106632f7f30fd2c047ccfe8539ad10cfccde4117db6b7a56ce0035653b4` |
| Runnable manifest in the exported archive | `sha256:419739d02247b8b2208f654e65b5118dfabc61afe909767f212f79773311fc17` |
| Image configuration identified by Syft and Grype | `sha256:e83d2c411b3b3d594f76ad2044bf4821fc15759ce2660cbccae44566f28698d8` |

The retained [index](evidence/bicep-crypto-2026-11-08/image-index.json) and [manifest](evidence/bicep-crypto-2026-11-08/manifest.json) bind these distinct identities. They are local evidence, not published registry references. Syft 1.54.0 scanned the exported OCI archive in a Linux container; Grype 0.120.0 scanned that SBOM with a valid database built at `2026-11-07T06:18:48Z`. The scan completed at `2026-11-07T23:25:07Z` (November 8 locally). The [SBOM](evidence/bicep-crypto-2026-11-08/sbom.syft.json.gz) is retained as gzip-compressed original bytes, with both compressed and original hashes in the index. The [Grype report](evidence/bicep-crypto-2026-11-08/grype.json) preserves findings and scanner settings; only two host cache paths and the output filename are redacted. The index records those fields and the original report hash.

The [runtime report](evidence/bicep-crypto-2026-11-08/runtime.json) passed all five offline checks: virtual-network and storage-account compilation, invalid parameters (`BCP036`), an uncached module version (`BCP190`) and missing cache configuration (`BCP190`). No probe containers remained. The six opt-in SDK tool-call cases in `tests/test_bicep_prepared_offline.py` also passed against the local image with networking disabled, including repeated calls, nested files and host policy configuration.

## Remaining findings and release boundary

There are no matches, including ignored matches, for either cryptography package in the fresh report. In particular, the issue's four advisories (`GHSA-7hqp-7c5m-4vq6`, `GHSA-r955-8q5j-3h83`, `GHSA-846f-f764-76mx`, `GHSA-c399-9853-6jmj`) are absent.

The [assessment](evidence/bicep-crypto-2026-11-08/assessment.json) remains **vulnerable**: `glibc-2.44` 2.44-r14 has High `CVE-2026-84243`, fix state `unknown`, with no scanner-known fixed version. The rebuild's `apk upgrade` therefore does not establish remediation of that advisory. Keep it visible in monitoring and recheck vendor package availability before publication. The raw SQLite High match remains in the report; the existing image-bound library assessment classifies it as not affected using the retained file hash and package inventory, expiring on December 4, 2026.

This work verifies a local replacement and prevents the cryptography regression in candidate inventories. It does not publish a replacement, change 0.2.1 or any other immutable release, qualify a deployed consumer, or establish ACAS, WSLC, Docker Sandboxes or production acceptance. A maintainer must prepare an unused image version, review fresh evidence and approve publication. #2522 remains open until a replacement release is available; consumers must verify and explicitly adopt its new digest.
