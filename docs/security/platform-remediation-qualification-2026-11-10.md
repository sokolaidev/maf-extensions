# Platform remediation rehearsal qualification: 2026-11-10

The explicit historical rebuild rehearsal passed image qualification, signed App orphan recovery and the generated draft PR's normal CI. This advances [#2517](https://github.com/sokolaidev/maf-extensions/issues/2517) under [#2514](https://github.com/sokolaidev/maf-extensions/issues/2514); it does not qualify a generated Bicep/APK update, current published-image recovery or production automation. The [manual rehearsal contract](platform-remediation-rehearsal.md) defines the isolation and evidence requirements.

## Run and retained evidence

[Run 42806754075, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42806754075/attempts/1) selected `historical-rebuild` and `exercise_delivery=true` on `main` at `ea0bda4ce1a18d4380c2a5327a9587f23a09b97e`, after [#2618](https://github.com/sokolaidev/maf-extensions/pull/2618). Acquisition, all six image jobs, the source gate, aggregation and App delivery completed successfully. The retained aggregate says `status: qualified`, `evidenceMode: historical-replay`, `productionRecovery: false`, with a `release-needed` plan and no source edits.

The finding fixture remains [monitor 42613419444/1](https://github.com/sokolaidev/maf-extensions/actions/runs/42613419444/attempts/1), producer `3117c76c9c51bcf5ebe209b62de8221ea6a690c0`, and its hash-pinned `bicep-docker-sbx-0.2.2` records. The request binds case-manifest SHA-256 `62c0aa324e7070e41632ea3c9dd76b80c21fff92e0812838825396f19dcf1589`. Separate current delivery authority authenticated [monitor 42791991947/1](https://github.com/sokolaidev/maf-extensions/actions/runs/42791991947/attempts/1) and retained all three Bicep profiles. Historical findings did not replace current release, source, monitor or age checks.

| Artifact | Retained result |
|---|---|
| [Plan 13758039657](https://github.com/sokolaidev/maf-extensions/actions/runs/42806754075/artifacts/13758039657) | Request, historical finding receipt, plan and separate current authority |
| [Qualification 13760983135](https://github.com/sokolaidev/maf-extensions/actions/runs/42806754075/artifacts/13760983135) | Acquired inputs, all six image result bundles and `remediation/rehearsal-qualified.json` |
| [Delivery 13760259961](https://github.com/sokolaidev/maf-extensions/actions/runs/42806754075/artifacts/13760259961) | `rehearsal-delivery.json`, including signed ownership, refusal probes and unchanged repeat |

These artifacts expire on 2026-12-10. This record preserves measured outcomes and identities, not the complete raw evidence after expiry. The SHA-256 values below bind the downloaded file bytes, not their ZIP containers.

| File | SHA-256 |
|---|---|
| Plan `rehearsal-request.json` | `79bb0884bc1a153b0a673f3ad98d76ccfbc945b59ad2a174e18a2b1633e8def4` |
| Plan `current/receipt.json` | `2837a135efaf4affce4565cd1c9900638bc09f21eee72ca2bf01dd95e63639b6` |
| Qualification `remediation/rehearsal-qualified.json` | `2d4401f5873d6cc47b33d137b4b3ffa93c38c5816b3048d9aa54caa0fad72f9c` |
| Delivery `rehearsal-delivery.json` | `d8fcb1c05356c9dc047a3b8849d78a5dfc619be46ad864347018eb3f74a25308` |

## Image and source qualification

Both variants build current source. They do not reproduce the historical vulnerable image or demonstrate a newly generated dependency change. Every SBOM contains `System.Security.Cryptography.Pkcs` and `System.Security.Cryptography.Xml` at `10.0.14`; every aggregate scan reports zero targeted findings. Raw residual findings remain retained: all six assessments are still `vulnerable` because of High `CVE-2026-84243` in glibc.

| Profile | Variant | Rebuilt image configuration ID |
|---|---|---|
| `bicep` | baseline | `sha256:ef9eff8f74ce9a6f0b2f1a52d7d02e9c75d9c2949a45345013f66a62643adc78` |
| `bicep-avm` | baseline | `sha256:9599280f4a85f7b7228bbf467436f6e6349605af6b407fe244e695d254a745ba` |
| `bicep-docker-sbx` | baseline | `sha256:41b009e0001988776a6de0e7c302ed844033a2336a44ecd49bf7f16a6edc2e5f` |
| `bicep` | candidate | `sha256:8d9ebd9904a6c98c4a1e43a3830d9fc24af052a4565aa4d1c5d05b70c7bc1547` |
| `bicep-avm` | candidate | `sha256:50414917e85af7345a28c2b9bc4c84ca07cb0b340a3eebc747e89403e426205d` |
| `bicep-docker-sbx` | candidate | `sha256:b97d1e5da8461c6d5371873ab4d4754a438187f3da353cd37168aa633a6ea1f0` |

All runtime records passed. `bicep` and `bicep-avm` cover offline packaging; Docker Sandboxes covers the SDK consumer, staged configuration, output transfer and cleanup. Every record says `liveDeploymentVerified: false`; this does not qualify ACAS, WSLC or production deployment. Component-scoped SQLite non-applicability remains bounded to the recorded library bytes and expiry in the Docker Sandboxes assessments.

The [hosted source gate](https://github.com/sokolaidev/maf-extensions/actions/runs/42806754075/job/129800513069) passed 28,155 tests, with 769 skipped and 56 warnings. Lint, formatting, Bandit, package/root type checks, documentation paths, implicit concatenation, Markdown and 414 tracker references passed. Bandit reported zero new findings and 270 baseline findings; root pyright reported zero errors and 619 warnings. These results belong to the recorded run source, not a later documentation change.

## Signed App delivery and normal PR CI

The delivery record says `delivery-passed`. Its initial branch head was absent. After the deliberate interruption before PR creation, it retained commit `1961129e2e6bab136890a7faf3947e4a5d21d60f`, with both author and committer identified as `sokolai-image-remediation[bot]`. The verified ownership record binds base `ea0bda4ce1a18d4380c2a5327a9587f23a09b97e` and the sole changed file, `tests/fixtures/image_dependencies/platform-rehearsal-receipt.json`, at SHA-256 `b22b9428d796f97925eb8aba91fea9ef5c10ce2f9a3db67a6b07fa0bfa14411a`.

Read-only ownership probes refused an unsigned commit response because it lacked a unique ownership marker and an altered response because it lacked a valid App signature. These were injected reads; no hosted ownership record was tampered with. Recovery created [draft #2635](https://github.com/sokolaidev/maf-extensions/pull/2635), and repeat delivery returned `unchanged` with the same URL and head. The draft changes only the receipt and is explicitly **do not merge**. Leave it available for evidence review; closing it is separate from this qualification record.

The delivery artifact deliberately says `ci: not-verified`. Subsequent inspection established the separate CI result at exactly `1961129e2e6bab136890a7faf3947e4a5d21d60f`: 33 checks succeeded and six were skipped, with no pending, failed or approval-required checks. All nine workflow runs completed successfully.

| Workflow | Exact-head run |
|---|---|
| Package and Sandbox Validation | [42810568898](https://github.com/sokolaidev/maf-extensions/actions/runs/42810568898) |
| Portable workflow tests | [42810569455](https://github.com/sokolaidev/maf-extensions/actions/runs/42810569455) |
| CodeQL advanced | [42810569371](https://github.com/sokolaidev/maf-extensions/actions/runs/42810569371) |
| Docs | [42810568994](https://github.com/sokolaidev/maf-extensions/actions/runs/42810568994) |
| Trackers | [42810569491](https://github.com/sokolaidev/maf-extensions/actions/runs/42810569491) |
| PR title | [42810569450](https://github.com/sokolaidev/maf-extensions/actions/runs/42810569450) |
| Image security | [42810568939](https://github.com/sokolaidev/maf-extensions/actions/runs/42810568939) |
| CodeQL - Code Quality | [42810566496](https://github.com/sokolaidev/maf-extensions/actions/runs/42810566496) |
| Automatic Dependency Submission | [42810133050](https://github.com/sokolaidev/maf-extensions/actions/runs/42810133050) |

## Positive-case selection

A separate local read-only acquisition on 2026-11-10 authenticated all 15 supported current platform releases and recomputed the production plan at source `12e99b7938885ecbbc249594b2f27fdcb6b339a0`. Its receipt records acquisition time `2026-11-10T13:09:06.159269+01:00`, monitor `42791991947/1`, catalogue sequence 1067 and catalogue digest `sha256:ab1ab59ccdbd72941c81f49470a5ae3b0f29c8ea1f07cd2f578f8a12cafcb1bc`. Two earlier attempts refused a stale checkout before the checkout was refreshed to current main; the successful attempt passed the final freshness check. This selection was not another hosted build or delivery run.

The resulting plan contains 17 APK findings, all `no-compatible-fix`, zero source updates and zero affected profiles. Thirteen rows concern `glibc-2.44` at `2.44-r14`; four concern `sqlite-libs` at `3.53.4-r0` in the antivirus image. No actionable .NET finding remains. Consequently, `bicep-update`, `apk-pinned` and `apk-unpinned` are unavailable in this authenticated snapshot. This establishes what the planner can qualify from these records, not that no upstream fix exists outside them. Local `plan.json` SHA-256 is `882a77ba42724e736a4be12851744731816edcc3d32057f2cc41c00686e89d76`; `receipt.json` SHA-256 is `bdd706fdbadeba97528a5542f71919761c0408a1890b0083ab7b39870e17a9f5`. These local selection files are separate from the hosted artifacts above; the immutable monitor release retains their underlying monitoring inputs.

The older tracked Bicep dependency manifest introduced at [`7c10a6cda33f6958f304da8bf10bf8a0ed1ebae2`](https://github.com/sokolaidev/maf-extensions/commit/7c10a6cda33f6958f304da8bf10bf8a0ed1ebae2) is also insufficient as a positive fixture for the replay's four cryptography findings: it already pins both packages to `10.0.13`, which those retained findings advertise as fixed. The historical published inventory at `10.0.10` is vulnerable, but replaying its findings against either that tracked manifest or current source does not generate an update. No production pin or acceptance guard was lowered to manufacture a baseline match.

## Remaining acceptance

Positive Bicep and pinned/unpinned APK update cases remain unqualified. They require authenticated compatible findings, signed vendor-package availability for APKs, a bounded generated proposal where necessary, the required baseline matches, candidate removal, inventories and runtime checks for every inherited profile. Delivering a receipt does not establish normal CI or signed delivery of the real dependency diff. A positive historical case needs its own reviewed source/evidence fixtures and an isolated destination; the successful rebuild fixture cannot silently become one.

`PLATFORM_IMAGE_REMEDIATION_ENABLED` remained unset after the run. No image was published and neither #2514 nor #2517 was closed. Automatic enablement, normal production operation, publication and current published-image recovery remain separate acceptance and maintainer actions.
