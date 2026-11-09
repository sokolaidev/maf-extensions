# Platform remediation qualification: 2026-11-09

This records partial hosted qualification for [#2517](https://github.com/sokolaidev/maf-extensions/issues/2517) under [#2514](https://github.com/sokolaidev/maf-extensions/issues/2514), after [#2577](https://github.com/sokolaidev/maf-extensions/pull/2577). The [platform remediation contract](container-image-releases.md#bicep-and-os-package-remediation) defines the acceptance boundary. Bicep rebuild qualification passed; platform App delivery, signed orphan recovery and actionable OS-package updates remain unqualified. Both implementation issues remain open.

## Run and retained evidence

[Run 42627135424, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42627135424/attempts/1) was manually dispatched on `main` at `26507ca3ca165830150999d8f3cc4a1805faf5df`. Acquisition, all six baseline/candidate image jobs, source checks and final aggregation succeeded. The final result is `release-needed`, with no proposed files and no unresolved targeted findings. Final freshness validation passed without deferral.

Acquisition authenticated [monitor 42613419444, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42613419444/attempts/1) and catalogue sequence 983, digest `sha256:6a3acce8b1a4b519ee7d2fed5d53c468d4588cc0f56ad880d71857d8c702469c`. That monitor concluded `failure` because vulnerable images remained; its completed evidence delivery was verified. A successful remediation run does not turn that monitoring assessment green.

The final [qualification artifact 13680423720](https://github.com/sokolaidev/maf-extensions/actions/runs/42627135424/artifacts/13680423720), named `platform-remediation-qualification-42627135424-1`, contains the acquisition receipt, plan, both aggregate results and all six build/runtime/SBOM/scan bundles. Its uploaded ZIP SHA-256 is `a4ac08f279dcc2cf6438a0d1d4a138c0778f55a6212df25add7cd817905de63c`. The workflow retains it for 30 days; this document preserves identities and measured outcomes, not the complete raw evidence after artifact expiry.

| File within the artifact | SHA-256 |
|---|---|
| `remediation/receipt.json` | `fecaf2e758b06fb613f48d8abc45020ec016eda8493c73505b94f556d8f62238` |
| `remediation/plan.json` | `19cfc0982753b720eb950842a5dccb9fc41c0ad27f51c96705f22661f30e1e09` |
| `remediation/qualification.json` | `e9d71c42368402219ce9d32849a57c891d62279ca1762a1cdbf0c4c37334da00` |
| `remediation/qualified.json` | `e9d71c42368402219ce9d32849a57c891d62279ca1762a1cdbf0c4c37334da00` |

To inspect this dated result, download that attempt's artifact, compare these file hashes, and read both aggregate files. For every entry in `scans`, match the source, profile, variant, plan digest and image ID to the corresponding `results/<variant>-<profile>/` build, runtime, SBOM and Grype records. The hosted aggregate also recomputed assessments, verified targeted package inventories and checked current evidence before writing `qualified.json`. Downloaded results were inspected for matching source/image identities, runtime success, cleanup, targeted counts and residual assessments. This inspection did not rerun the hosted builds or establish current catalogue freshness after the run.

## Measured Bicep scope

The planner retained four .NET findings from the published `bicep-docker-sbx` image as `source-pin-sufficient`: `GHSA-846f-f764-76mx`, `GHSA-7hqp-7c5m-4vq6`, `GHSA-c399-9853-6jmj` and `GHSA-r955-8q5j-3h83`. The published inventory contained version `10.0.10`; the reviewed source policy already pinned `System.Security.Cryptography.Pkcs` and `System.Security.Cryptography.Xml` to `10.0.14`. It selected all three inherited Bicep profiles and proposed no source changes. Seventeen other finding rows remained `no-compatible-fix` across the acquired evidence.

Both variants build current source through the ordinary source-route recipes; the baseline is not a rebuild of the old published compiler. Consequently, both variants already contain the fixes. This proves source-already-fixed/release-needed handling, not a newly generated patch removing matches between baseline and candidate.

| Profile | Variant | Rebuilt image configuration ID |
|---|---|---|
| `bicep` | baseline | `sha256:4e664c49dd7890e6366bc3a8c2e6e6cb81ea0a314b0bf009ef1c20a7365932dd` |
| `bicep-avm` | baseline | `sha256:1c97ee20f803c321fab60013c3b67e63f0263e38a652119c31adf74664f8738c` |
| `bicep-docker-sbx` | baseline | `sha256:a11c35b284e53e54bdff751f7759b78a74d7d1247668a6373cf7ec35faad72c3` |
| `bicep` | candidate | `sha256:56ca6be27c4334952ea15c900cd05147e8af8b4ecbd6f8be73f392d3df2c519b` |
| `bicep-avm` | candidate | `sha256:05eb1812f3a750c9a34d8597d43f920b0e7bba6de32c1da82b140da2164120d4` |
| `bicep-docker-sbx` | candidate | `sha256:81bad23be917259ddfd64e28021e8a737c85e084d3f771f958a591a3796212fc` |

All six Linux/amd64 runtime records report success and empty container or sandbox inventories. Compiler receipt and inventory validation passed for the source route, including the two cryptography packages at `10.0.14`. Docker CLI packaging probes cover `bicep` and `bicep-avm`; `bicep-docker-sbx` exercises the Docker Sandboxes SDK consumer with staged configuration, output transfer and cleanup. These probes do not establish ACAS, WSLC or production deployment qualification.

Each aggregate scan entry reports zero targeted findings. Each assessment still reports `vulnerable` with High `CVE-2026-84243` in glibc. Scans used Syft `1.54.0`, Grype `0.120.0` and valid database schema `v6.1.10`, built `2026-11-08T06:19:57Z`, with distribution checksum `dcca24722e16c2bcf4a542a3f7fdd2147baf9215c17b973f5a7d4243fbddacea`. Raw lower-severity matches and any component-scoped assessments remain in the bundles. No new suppression or vulnerability-free claim is made. Under the existing release policy, High findings remain warnings; applicable Critical findings and unavailable evidence block publication.

The [hosted source checks](https://github.com/sokolaidev/maf-extensions/actions/runs/42627135424/job/129199696797) passed at the recorded source: 27,849 tests passed, 762 skipped and 56 warnings; lint, formatting, Bandit, package/root type checks, documentation paths and implicit concatenation checks completed. Markdown checks and 403 tracker references also passed. This is evidence for that source and plan, not CI validation of a later documentation PR.

## Remaining qualification plan

`PLATFORM_IMAGE_REMEDIATION_ENABLED` was unset. Token minting and proposal delivery were skipped. No App signature, platform PR, orphan recovery, image publication or deployment resulted. The successful freshness check does not by itself prove a deliberately concurrent unrelated publication or a stale-input rejection. Repeating this empty proposal with writes enabled would still not qualify PR creation.

The following work remains under #2517 before claiming automatic platform remediation operational:

| Step | Required execution | Acceptance evidence |
|---|---|---|
| 1. Prepare controlled cases | Select authenticated evidence with a compatible Bicep patch and pinned/unpinned vendor APK fixes. If current evidence has no such fix, prepare a separately reviewed rehearsal harness using explicitly identified historical fixtures and an isolated branch/PR destination. Keep production source pins, current-evidence checks and catalogue state intact. | Record source and fixture hashes, affected profiles, vendor/base identities and expected diffs. Label rehearsal results separately from current published-image recovery. Missing compatible cases remain pending. |
| 2. Exercise actual updates and rebuilds | For a pinned APK case, install the advertised fixed package from the pinned vendor base's signed repositories, apply every build-consumed occurrence and rebuild all inherited profiles. For an unpinned APK case, run the ordinary upgrade recipe without inventing a source edit. Exercise a nonempty Bicep NuGet proposal through the same runtime and inventory gates. | Retain vendor installation output, baseline/candidate SBOMs, exact image IDs, runtime cleanup, compatible package versions and disappearance of the targeted matches. Retain unrelated findings; the unpinned case must report `release-needed` when rebuilding alone suffices. |
| 3. Rehearse App delivery and recovery | Add a reviewed manual-only qualification mechanism before exercising writes while automatic triggers remain disabled. Use the dedicated App and an isolated rehearsal destination for a qualified nonempty proposal. Create one PR, repeat unchanged delivery, then exercise interruption after the signed branch write but before PR creation and recover from fresh qualified input. Exercise modified/unsigned ownership refusal without changing a human-owned branch. | Retain App identity, source/tree/file hashes, signed ownership record, expected-head transitions, PR URL/head, attempt-specific artifacts and normal PR CI. Repetition must preserve one PR; recovery must verify the trusted signature and refuse altered ownership. Never retain private keys or tokens. |
| 4. Record acceptance and enable normal operation | Review the controlled results and remaining boundaries, then explicitly enable `PLATFORM_IMAGE_REMEDIATION_ENABLED` for normal monitor/scheduled delivery. Observe the next normal run and reconcile the #2514/#2517 status rows and parent checklist. | Link the evidence PR and exact runs; close only the implementation scope actually demonstrated. Preserve release requests and residual-vulnerability tracking independently. |

The immediate follow-up implementation is the manual-only rehearsal mechanism and its case manifest from steps 1 and 3; the current workflow has no such isolated mode. A rehearsal must not pass historical fixtures off as fresh production evidence or downgrade production inputs to force an update. A successful rehearsal qualifies its stated path; current-image recovery still requires separately approved publication and fresh monitoring. Automatic enablement, publishing, environment approval and issue closure are not performed by this evidence record.
