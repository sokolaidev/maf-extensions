# Trusted-main consumer qualification: 2026-11-06

This is the hosted evidence record for [#2421](https://github.com/sokolaidev/maf-extensions/issues/2421), following [#2419](https://github.com/sokolaidev/maf-extensions/pull/2419). It covers the 11 declared Linux/amd64 Docker profiles at source `b0bc8f90a6e9389b9145293534b1f6da9b425853`. The [qualification contract](container-image-releases.md#independent-consumer-qualification) defines the trust and compatibility policy.

## Execution and signing

[Run 42212383553, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/attempts/1) was manually dispatched on `main` with `profile=all`. Selection, all 11 execution jobs and all 11 fresh signing jobs succeeded. Each signing job downloaded its profile artifact, recomputed the canonical input subject and validated the transferred source, run and attempt before issuing the custom attestation. No profile was blocked or unrun in this attempt.

Every record reports `passed: true`, the expected workload configuration ID and an empty top-level container inventory. All resource inventories present in the detailed runtime records are empty. The scope column below describes the existing probes: a packaging check does not establish SDK execution or a live cloud deployment.

| Profile | Probe scope | Published workload registry digest | Workload configuration ID |
|---|---|---|---|
| `antivirus` | `offline-sdk-consumer` | `sha256:596ceacf6d3d8db2804689462a27e777f614f562645ee070288928f07386a1de` | `sha256:280a613e3e91456081c3308771a66ea63a7323318795996dc1605b3863aeb7e2` |
| `azure-pricing` | `live-public-api-sdk-consumer` | `sha256:8c50c528d9c93a2d6d91087ae722a3f8e6e629f173c6bd67fc43eee46bd8baad` | `sha256:e7fb57560b807471cd4129d51cd2da074a1e0a159e4a39e2aa69d62e46346090` |
| `bicep-avm` | `offline-packaging` | `sha256:19d2fe384dd701a823b26df71a250f604e832681a57abb5f60c2e4a185d35fb4` | `sha256:603e19f1c29c221ecacb90c56b98d639b1c3b46c485fd0e8e59c8b88d8868a50` |
| `bicep` | `offline-packaging` | `sha256:940ef6b9ed7d4db78a3e458374998394d063b13d0550fb426b9311004a934ad3` | `sha256:ec73e68cad2afa13abed0546412b71dcbcf43213ac82bfe8b4cfe93117d13c87` |
| `document` | `offline-sdk-consumer` | `sha256:9cf6451dddf030f3ac977f10c3f578fd620857eac43bc1e87f401fbca34b6c3d` | `sha256:45c94d991d3d3cc91f8bdc095580728356daaecbbab18d936d68d016e00f37f0` |
| `drawio` | `offline-sdk-consumer` | `sha256:39a30f7171a661dd12f3f4d7b78033bf57c41403707ca634fa08614706f9c1a9` | `sha256:3c554148cc6ac27d5d3588ca1dc67643e0502378598fd3a575735a1060bf0724` |
| `drawio-export` | `offline-sdk-consumer` | `sha256:bd590ba58da74254c861f940317cba4831faf6fd8fbda0f7e19fc802b4708077` | `sha256:d9474c8cdfd4858dae7012cc396c0ede276e9a25e2b12192edc7546dd368aaaa` |
| `opentofu-random` | `offline-packaging` | `sha256:9b1d46dc70f87827599438b10d5e28a9f5e7a64626a31c184f00a3e71cafe901` | `sha256:385646906afada3204025ad879b50fa5f9d586feb5ac1239e2b358052a282d04` |
| `shell` | `offline-sdk-consumer` | `sha256:861d499ab2159fa218d6ebff05c97d45bc9a05a79563b31ad5fbe8ec032dff6a` | `sha256:17213fc6663b049f19f188539044d1dbdbf13fdcd4c1066a192a8a8d4f9d7dc4` |
| `terraform-avm` | `offline-packaging` | `sha256:83a0199f006fec6abe6f2b9999310d44c338594203490e1ae622b36de83bdc7f` | `sha256:de8f3f8e41d9a02d39e66cb5de7b6528159164a081106b590ea262fde506a0b5` |
| `terraform-random` | `offline-packaging` | `sha256:a01d4aa890b9ea0438a5419879c727fb2a448fd99ede824586c36a21fc7620f3` | `sha256:a253d16a5f520268cc1234ecb4ad954a5c4f4a041f45e9e15c52bd1eb525a6df` |

Attempt 1 used separate pricing proxy configuration `sha256:14cdc8b7f2ca0465e0181bd418ff8de28babeae79ace3b916b46dcc02d1c14f9`.

Pricing's Reservation and Consumption queries returned trusted rows, both `unlisted-host` and `post-method` probes were denied, and its container and network inventories were empty. Its separate proxy configuration identifies locally built proxy bytes, not a published proxy manifest. The signed predicate retains the full pricing cases, installed distributions, Python/Docker environment and timings.

## Full workflow rerun

[Run 42212383553, attempt 2](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/attempts/2) reran the complete workflow at the same source. All 23 jobs succeeded again, including all 11 input-validation/signing pairs. The 11 original artifact IDs were replaced by 11 different IDs under the same names, with no upload conflict. Every downloaded replacement record names run `42212383553`, attempt `2`. The canonical subject bytes, workload registry digest and workload configuration ID are identical between attempts for every profile. No declared profile was blocked or unrun in either attempt.

The first-attempt IDs below were retained before replacement and no longer identify current downloadable artifacts. The subject digest is SHA-256 of the canonical file bytes and is shared by both attempts.

| Profile | Replaced attempt-1 artifact ID | Attempt-2 artifact | Canonical subject SHA-256 |
|---|---|---|---|
| `antivirus` | `13498964200` | [13499249147](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13499249147) | `b07a48cd97f4b4cc0e14f3605fb95ea1d438f56c0f2f0fd9aa2bcf35859c8c5e` |
| `azure-pricing` | `13499163718` | [13499173752](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13499173752) | `09793d41244a853a7b3f9b57bcd5e4d5cf5ae2fecdbdf2e6aa312228567169f4` |
| `bicep-avm` | `13500495836` | [13499283089](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13499283089) | `bd37be10b1fcb40d98aa69ad355f7508b88d1486a8800496bf34c83507c88fca` |
| `bicep` | `13499122433` | [13500641235](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13500641235) | `7ad28a37673ba84ade42e1a9e87a6cf0ce62886a101be44671fc692c1a070cca` |
| `document` | `13499053761` | [13499414148](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13499414148) | `ebbe7829e5d17dd080b2d788aa26c92667dd501c5a68fb7f124e2956dac8cc2b` |
| `drawio` | `13499756664` | [13500670424](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13500670424) | `c8483f44c82398eb6ffda34da3ae6a73301832e252dd1880dee35726978cd620` |
| `drawio-export` | `13500525900` | [13500387584](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13500387584) | `01bd8d0555c6fe767c97fcd2aff2877b5659dc1a4a8511afeed94a3292e528bb` |
| `opentofu-random` | `13498998153` | [13498984036](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13498984036) | `222f937976ea061bbcbef85552575cd0df10c8e3fd892c95dc3b55f9d6a64275` |
| `shell` | `13500680084` | [13500805171](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13500805171) | `51dc2c29c5ed1bfa78eacc065d0d81d9dd8af603848c4b7a979fecab969a4b0c` |
| `terraform-avm` | `13499308370` | [13500276945](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13500276945) | `76c158f76a0b0067ae8032808efa235bc1dd401a46c8f594fcf85074c59bac0d` |
| `terraform-random` | `13500615009` | [13499427942](https://github.com/sokolaidev/maf-extensions/actions/runs/42212383553/artifacts/13499427942) | `0d5514a76a1ef1c5717c4716ac1ee0387ffc5cdf0806e60588bc0e9f34f19830` |

Attempt 2 used pricing proxy configuration `sha256:b2e026ff843aa84804a40abcdfb161845b5d53eaec6e2c6b5230dfdd9cd26f01`. Its pricing and denial probes passed again, with empty container and network inventories. The separate proxy configuration may change between runs without changing the published workload identity.

## Independent verification

The readiness verifier retrieved and accepted all 11 records from both attempts of run 42212383553, as well as earlier [run 42201560885, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42201560885/attempts/1) at the same source. Each downloaded predicate was also found verbatim in the source-pinned GitHub verification output. For every profile in the rerun, readiness returned attempt 2 and the authenticated certificate's `runInvocationURI` identifies `42212383553/attempts/2`; the source and signer digests both equal the recorded source, the ref is `refs/heads/main`, and the runner is `github-hosted`. The attempt-1 certificates were checked against attempt 1 separately. This is attestation retrieval and verification, separate from hosted execution and PR CI.

The complete `check_kind_release_images.check` readiness check explicitly selected all seven kinds and accepted all 11 current published profiles, including authenticated original release evidence. Bicep, Bicep AVM, drawio, drawio-export, Terraform random and OpenTofu random used independent consumer evidence from run 42212383553. Pricing, antivirus, document, shell and Terraform AVM reused compatible original image-release evidence. The direct consumer-verifier checks above separately exercise independent attestations for all 11 profiles; the complete readiness result is not a version-selection no-op.

Verification reconstructs `consumer-inputs.json` from committed inputs and the workload digest/configuration ID, then calls `consumer_qualification.verify`. The GitHub CLI enforces the canonical subject digest, predicate `https://github.com/sokolaidev/maf-extensions/consumer-qualification/v1`, certificate identity `https://github.com/sokolaidev/maf-extensions/.github/workflows/kind-consumer-qualification.yml@refs/heads/main`, issuer `https://token.actions.githubusercontent.com`, `refs/heads/main`, GitHub-hosted runners, and both source and signer digests equal to the recorded source commit.

To retrieve and verify one retained subject, use the following commands; substitute another `consumer-<profile>-docker` artifact name to inspect another profile. Downloaded Actions artifacts have 90-day retention. Attestations remain retrievable by the canonical subject digest independently of that retention; reconstruct the subject with `consumer_qualification.inputs` at the recorded source and workload identity when the artifact is no longer available. Preserve the original bundle bytes when using `gh attestation download` for local verification.

```bash
gh run download 42212383553 --repo sokolaidev/maf-extensions --name consumer-antivirus-docker --dir qualification
gh attestation verify qualification/consumer-inputs.json \
  --repo sokolaidev/maf-extensions \
  --cert-identity https://github.com/sokolaidev/maf-extensions/.github/workflows/kind-consumer-qualification.yml@refs/heads/main \
  --cert-oidc-issuer https://token.actions.githubusercontent.com \
  --source-ref refs/heads/main --deny-self-hosted-runners \
  --predicate-type https://github.com/sokolaidev/maf-extensions/consumer-qualification/v1 \
  --source-digest b0bc8f90a6e9389b9145293534b1f6da9b425853 \
  --signer-digest b0bc8f90a6e9389b9145293534b1f6da9b425853 --format json
```

The CLI may return several valid records for the same unchanged input subject. Match the downloaded predicate to `verificationResult.statement.predicate`, then compare its `runId` and `runAttempt` with the authenticated certificate's `runInvocationURI`. A valid older record is not evidence that a newer attempt signed successfully.

## Rejection checks

The following local checks use the authentic downloaded antivirus record from run 42201560885, attempt 1. They do not issue attestations or modify hosted records. The original downloaded bundle passes verification; each altered policy or payload below is refused.

| Probe | Observed result |
|---|---|
| Wrong source digest or signer digest | GitHub CLI exits 1 for each certificate binding |
| Wrong workflow identity or source ref | GitHub CLI exits 1 for each policy mismatch |
| Wrong predicate type | GitHub CLI exits 1 |
| Changed subject registry digest with the original bundle | GitHub CLI exits 1 |
| Lookup for the modified subject with no matching evidence | `consumer_qualification.verify` raises the unavailable-evidence refusal |
| Changed signed predicate attempt without a new signature | GitHub CLI exits 1 |
| Authentic attempt-1 transfer replayed through `validate_for_signing` with attempt 2 | Local validator refuses the workflow-attempt mismatch; no signing is attempted |

The stale-transfer check is a local replay of the deployed validator with workflow environment values, not an attempted injection into a hosted signing job. Previously issued evidence for unchanged inputs remains valid for readiness; the current-attempt rule governs new signing transfers.

## Qualification boundary

This record establishes only the existing Docker probes and the trusted-main consumer-attestation chain for the recorded inputs. It does not establish ACAS, WSLC, Docker Sandboxes, other backends, production acceptance, or a fresh vulnerability assessment. No image or package publication is part of this validation. Later changes to fingerprinted consumer inputs require their own matching evidence.
