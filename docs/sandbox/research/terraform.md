# Terraform and OpenTofu research

> Consolidated research record, 2026-09-14 through 2026-09-16. It combines the CLI, implementation, dependency-preparation, egress, Azure Verified Module (AVM) catalog and provider-linking investigations for [`maf-sandbox-terraform`](../kinds/terraform.md). The validation kind and approved dependency preparation are implemented; catalog-wide baking and unpacked provider linking remain follow-up work.

## Decisions at a glance

The first workload is a validation sibling for Bicep, not a general infrastructure lifecycle tool. It supports Terraform and OpenTofu behind one implementation, stages an explicit input snapshot, runs initialization, validation and formatting, and reports initialization incompleteness separately from validation failure. Planning, applying, destroying, importing, state commands, variable-dependent initialization, policy tools and warm reuse remain outside the first-version scope.

The initial runtime profile has closed egress, call-scoped disposable sandboxes, host-controlled engine images and a filesystem provider mirror with no direct fallback. Local modules may be staged with the input; remote and dynamic module sources are not runtime dependencies. Providers are executable programs, and configuration expressions can read outside the staged project, so the workload makes no call-directory confinement or passive-configuration claim.

Dependency preparation happens outside the validation sandbox. A trusted host manifest pins provider identities, versions, platforms, archive digests and provenance, while separate module graphs pin local bundles or registry-resolved module packages. The preparer verifies complete artifacts and builds an immutable image. The guest receives no download interface, and validation remains closed-egress.

The measured AVM catalog is large enough for one shared image, but not with the current one-version-per-source graph model. A generated, reviewed manifest, multiple versions per module source, subdirectory support, a Terraform-compatible module reader and an unpacked provider mirror are needed before catalog-wide baking is practical.

## Validated workload

### Contract

The package is `maf-sandbox-terraform`, imported as `maf_sandbox_terraform`. The host chooses exactly `engine="terraform"` or `engine="opentofu"` at attachment time; `"tofu"` is not a third engine. The selected executable, tool name and report metadata remain engine-specific, and a missing requested binary is an error rather than a fallback to the other engine.

| Engine | Guest executable | Spec kind | Tool |
| --- | --- | --- | --- |
| Terraform | `terraform` | `terraform` | `terraform_validate` |
| OpenTofu | `tofu` | `opentofu` | `opentofu_validate` |

The workload requires `EXEC` and `FILES_IN`, assumes a POSIX guest, uses at least container isolation, creates one sandbox per call, disposes it after the call and requests `CLOSED` egress. The engine, provider mirror and launcher live in the image; the Python package does not bundle those binaries.

The host supplies a complete file manifest and a root module. The kind reads each original file through the session, preserves relative paths below a fresh `project/` directory and stages the selected root without writing to the host store. It refuses absolute paths, traversal, normalized-name collisions, runner-owned paths, missing or over-limit files, failed transfers, omitted configuration siblings and a root with no recognized configuration file. It also refuses state, saved plans, `.terraform/`, credential files, plugin executables, user CLI configuration and unsupported variable-file inputs. Ancillary text files needed by `file()` and `templatefile()` are allowed explicitly.

Terraform recognizes `.tf` and `.tf.json`; OpenTofu additionally recognizes `.tofu` and `.tofu.json` with its own precedence rules. Terraform mode refuses OpenTofu configuration files rather than validating only the files Terraform happens to read. The wrapper preserves filenames and does not concatenate or rename them. Missing local modules and unavailable mirrored providers remain initialization failures; they are never replaced with stubs.

The fixed phases are:

```text
<engine> init -backend=false -input=false -no-color
<engine> validate -json
<engine> fmt -check -recursive -no-color
```

Initialization and validation run from the selected root. Formatting runs from the staged project so sibling local modules are covered, while `TF_DATA_DIR` stays outside the project. `fmt -check` never changes source bytes. Failed initialization stops validation and reports `INITIALIZATION FAILED — VALIDATION INCOMPLETE`.

The launcher selects only the allowlisted engine and phase, derives private absolute paths, and builds a clean environment. It removes inherited `TF_CLI_ARGS*`, `TF_VAR_*`, `TF_TOKEN_*`, credential helpers, logging destinations, provider overrides and cloud credentials; it keeps `HOME`, `TMPDIR`, `TF_DATA_DIR` and the controlled CLI configuration inside the disposable call. Output is bounded, stdout and stderr are drained under one ceiling, process groups are supervised, and a single deadline covers all phases. Cancellation waits for cleanup within that bound before the core disposes the instance.

The validation JSON is parsed independently of the process exit code. The parser requires a supported 1.x `format_version`, correctly typed fields, consistent `valid` and diagnostic counts, useful locations and agreement with process status. Missing, malformed, truncated, oversized or contradictory output is incomplete execution, not a pass. Warnings are retained. Results use the existing split-result pattern: an untrusted derived report and one fixed standing-guidance item explaining that hidden or incomplete output does not establish a pass.

### Dependency and information-flow boundaries

The image uses a controlled filesystem mirror containing only selected providers and no `direct` installation fallback. Built-in `terraform_data` needs no external provider; the measured `random` profile loads a mirrored provider with networking disabled. A missing provider, an incompatible lock or an unavailable module causes incomplete initialization.

A supplied `.terraform.lock.hcl` is used read-only. Without a supplied lock, initialization may create one only in the disposable guest and reports the selected versions; it is not returned to the file store implicitly. Terraform and OpenTofu use the same lock filename but can use different registry addresses. For `hashicorp/random`, OpenTofu attempted to migrate a Terraform lock to its own registry address and refused when the lock was read-only. That is a dependency mismatch, not a reason to retry without the lock policy.

Closed egress stops downloads and remote API access but does not make configuration passive. The measured `file()` fixture read a known file outside its call directory, and both engines started provider processes during validation. Keep credentials and unrelated data out of the guest and dispose every call. All call-derived results are labelled untrusted, including verdicts, initialization output and diagnostic text; host file provenance is passed through the session rather than read directly.

### Direct CLI compatibility measurements

The standalone probe used Terraform 1.16.2 and OpenTofu 1.12.6 on Linux amd64 with HashiCorp `random` 3.7.2. Docker execution used a read-only root filesystem, closed networking, dropped capabilities, no-new-privileges, two CPUs, 768 MiB memory, 128 PIDs and a 384 MiB writable tmpfs. No credentials or host directories were mounted.

| Fixture | Terraform 1.16.2 | OpenTofu 1.12.6 |
| --- | --- | --- |
| Root plus sibling local module with an unset required variable | init succeeds; valid | init succeeds; valid |
| `.tf.json` built-in resource | valid | valid |
| Undeclared variable | validation JSON error | validation JSON error |
| Syntax error, missing local module or missing mirrored provider | initialization fails; validation skipped | same |
| Mirrored `random_string` and wrong attribute type | valid / provider diagnostic | same |
| Read-only initialization without a provider lock | initialization fails | initialization fails |
| Read-only reinitialization after generating a lock | succeeds | succeeds |
| Partial S3 backend with `-backend=false` | valid | valid |
| Only an invalid `.tofu` file | valid because Terraform ignores it | JSON error |
| Invalid `.tf` plus valid `.tofu` | JSON error | valid because `.tofu` takes precedence |
| Valid semantics with formatting mismatch | validation 0; fmt 3 | validation 0; fmt 3 |
| `file()` reads a known outside file / missing counterpart | valid / JSON error | valid / JSON error |
| Terraform lock used by OpenTofu in read-only mode | source lock | migration required; initialization fails |

There were 31 engine/fixture combinations. Every executed validation returned JSON format `1.0`; both version commands used the key `terraform_version`, which does not identify the engine. No state file appeared in a fixture call tree. The measurements cover the named versions and one external provider only: they do not establish Azure/AWS provider compatibility, remote module installation, general filesystem or process confinement, supported version ranges, Windows guests, router cleanup, cancellation, ACAS/WSLC execution or cloud deployment.

## Implementation evidence

The implementation verification used Windows with CPython 3.12.14 and Docker Desktop's Linux engine. Guest images pin Python 3.13.15, Terraform 1.16.2 or OpenTofu 1.12.6, Linux amd64, and the selected registry's `random` 3.7.2 archive.

| Evidence | Result |
| --- | --- |
| Deterministic workload tests | 60 passed, covering staging, path and transfer refusals, limits, provenance, hidden names, report parsing, engine selection and cancellation ordering through core sessions |
| Real Docker adapter matrix | 24 cases passed initially; after rebasing, 26 cases passed for both pinned images, with repeated calls, daemon-observed disposal and unchanged store content |
| Linux launcher tests | Two image runs passed, covering subprocess supervision, environment isolation, bounded output, inherited pipes, deadlines, locks and source/state nonmutation |
| Checkout examples | Both engines passed local-module validation in builtin images, mirrored-provider validation in random images and formatting checks |
| Packaging | Wheel-from-sdist, Twine metadata checks and isolated wheel installation/import smoke passed with a local core override |

The adapter scenarios covered local modules, JSON configuration, undefined-variable diagnostics, invalid syntax, unavailable modules, successful provider schema loading, provider type errors, formatting-only changes, wrong-engine images, cancellation, OpenTofu precedence and deadline expiry. Terraform refused a `.tofu` manifest at admission and created no container. Every admitted repeated call had a different container identity, and disposal was verified with `docker ps --all` rather than only the router ledger.

The latest unfiltered repository gate reached 9,028 passed, 371 skipped and seven test-stage failures. Two documentation-structure failures were corrected; the remaining five were unchanged Windows timing failures reproduced from untouched main, including process cleanup deadlines, retained-candidate batching and an ACAS client-pool lease timeout. Lint, formatting, strict package typing, root typing, documentation paths and Markdown Python-block checks passed separately. The default gate definition was not changed.

These are local results; remote CI is tracked separately by implementation PR [#1250](https://github.com/sokolaidev/maf-extensions/pull/1250). ACAS, WSLC, other guest architectures and other providers are not qualified for the builtin or random profiles. The prepared AVM profile has separate Docker and live-ACAS evidence described below.

## Dependency preparation and registry modules

### Why preparation is outside the sandbox

The implemented choice is a host-controlled preparation CLI, not a guest-facing network mirror. A trusted operator supplies the manifest; configuration, providers, agents and guests cannot submit requests to the preparer. The output is verified input for an immutable image build and explicit file staging. No engine or provider executable runs on the application host during preparation.

A network mirror would still require a guest request-content boundary. Direct downloads would require TLS inspection and bypass-resistant routing across Docker, WSLC and ACAS. Those controls are not portable and do not belong in the initial runtime contract. Opening hosts alone would not change the current runtime: the CLI configuration has only `filesystem_mirror`, and no `direct` fallback.

### Manifest and artifact rules

The manifest pins the engine, complete provider source addresses, exact versions and platforms, ZIP SHA-256 digests and operator-supplied provenance. Provider addresses remain intact in the mirror. Digests come from independently trusted release metadata; the preparer verifies bytes but does not establish the truth of a provenance assertion or verify signatures.

Each provider or module artifact has one exact HTTPS URL and, optionally, a reviewed redirect chain. GitHub release downloads may authorize one server-issued redirect to a repository-bounded release-assets path; the guest never receives the redirect or its signed query. Discovery, arbitrary credentials, Git transports, dynamic URLs and unrestricted CDN rules are refused.

Ordinary requests are canonical ASCII HTTPS URLs with lowercase DNS hosts, implicit port 443, exact paths, no userinfo, fragments, caller queries, percent encoding, dot segments, duplicate separators or backslashes. Requests are fixed GETs with fixed headers and no body. The transport ignores environment proxies, checks every DNS answer for disallowed address classes, connects to a checked numeric address without a second DNS lookup and verifies TLS against the approved hostname. Redirects, response headers, bytes, archive expansion and wall time are bounded; failures use fixed decision codes without returning URLs, bodies or signed tokens.

Provider ZIPs and module archives are processed as bounded data without extracting host filesystem entries. Links, special files, traversal, case collisions, oversized expansion, invalid text, duplicate JSON keys, hidden files, state, variable files and remote or dynamic module sources are refused. A receipt binds the canonical manifest, artifact and file inventories, graph, preparation implementation digest and effective limits. The image build starts from an empty trusted mirror, verifies the receipt and stages only the listed files.

The current implementation uses a parent and worker under one 180-second wall-time bound. Key limits are a 1 MiB manifest, 32 provider archives, 16 ordinary module archives, 16 registry module archives, 64 MiB per download, 256 MiB accumulated downloads, 1 GiB provider expansion, 8 MiB module expansion, 256 files per module bundle, 64 graph nodes and 256 inventory records below one registry module call. These limits are intentionally preparation limits, not a guest API.

### Local and registry module graphs

The initial local-module mechanism pins immutable revisions and archive digests separately from provider locks. It checks every selected configuration directory and local edge, refuses cycles and escapes, preserves bytes and does not resolve an undeclared remote child. The module archive can include ancillary text needed by Terraform, but it does not execute bundled scripts.

Registry modules such as AVM keep their authored source strings and version constraints; they are not rewritten to local paths. A prepared package is identified by source and exact version, with a pinned commit, archive digest and graph of module directories. The preparer verifies the graph and provider requirements. The image stores the selected package files and the inventory of `modules.json` records Terraform would otherwise write below one call.

At runtime, the launcher reads the staged project's module calls, follows local calls inside the project and writes records for matching baked registry packages using the authored source spelling. Terraform still checks the source and version constraint. A call it cannot read, a missing or incompatible package, a missing nested child or provider, or a lock checksum mismatch fails initialization, yielding an incomplete result. The base CLI configuration disables module registry discovery, so a fallback cannot silently turn into an online pass.

Terraform 1.16.2 reuses a module record only when its source string matches exactly and its version satisfies the constraint. A record without a version skips that check; a local module call without its own record can delete records below it; and `azure/...` does not match `Azure/...`. The launcher therefore preserves authored spelling, records local ancestors and writes the complete nested inventory. A small reader is used where full HCL parsing is unnecessary; a misread can only lose a record because Terraform rechecks it and the disabled registry fallback then fails.

A real `Azure/avm-res-network-virtualnetwork/azurerm` 0.22.2 graph was prepared and measured. It installed `interfaces`, `peering`, `subnet` and `subnet.interfaces`, with azapi 2.12.0, modtm 0.4.0 and random. Preparation took 4.5 seconds. The Docker and live ACAS CLOSED-egress probes both passed exact and `~>` pins, a local wrapper module and a correct read-only lock. An incompatible version, unbaked module, removed nested child, removed provider and wrong lock hash all failed initialization and rendered INCOMPLETE. Docker used network mode `none`; ACAS resolved DNS and accepted TCP/TLS to the registry through its proxy but returned 403, so the probe checked for retrieved content. Every call disposed its sandbox and left the store unchanged.

The prepared-image workflow and its full request contract live in the [image guide](../../../images/terraform-sandbox/README.md#approved-dependency-preparation). The open follow-up [#1249](https://github.com/sokolaidev/maf-extensions/issues/1249) covers approved artifacts and request paths; runtime validation continues to use closed egress.

## Future online egress profile

The current kind has no runtime egress dependency. The following hosts were observed only to define a future, reviewed dependency profile; they are not an approved allowlist and do not establish that arbitrary provider code is network-free.

| Scope | Observed HTTPS destinations |
| --- | --- |
| Terraform with sampled HashiCorp providers | `registry.terraform.io`, `releases.hashicorp.com` |
| Terraform with sampled providers plus AzAPI | `registry.terraform.io`, `releases.hashicorp.com`, `github.com`, `release-assets.githubusercontent.com` |
| OpenTofu with the four sampled providers | `registry.opentofu.org`, `github.com`, `release-assets.githubusercontent.com` |
| GitHub module archive URLs | add `github.com` and `codeload.github.com` |
| GitHub HTTPS Git module sources | `github.com`; registry-addressed modules also need their module registry |

The sampled provider scope was `hashicorp/random` 3.7.2, `hashicorp/azurerm` 4.0.0, `hashicorp/azuread` 3.0.2 and `Azure/azapi` 2.0.1 on Linux amd64. Registries provide discovery and metadata, but package URLs can point elsewhere: Terraform used HashiCorp releases for the sampled HashiCorp providers, while OpenTofu used GitHub release assets; AzAPI used GitHub through both engines. GitHub release assets redirected to `release-assets.githubusercontent.com`, so permitting only `github.com` is insufficient.

Both registries resolved `Azure/avm-res-resources-resourcegroup/azurerm` 0.2.0 to a GitHub commit. Terraform returned HTTP 204 with `X-Terraform-Get`; OpenTofu returned HTTP 200 with a JSON `location`. The returned source used Git over HTTPS. A separately requested archive used `codeload.github.com`, but that does not prove the registry's Git route needs the archive host. Git HTTPS reference discovery and POST to `git-upload-pack` mean GET/HEAD-only filtering is insufficient. Nested modules, submodules, S3/GCS/OCI/GitLab/Bitbucket sources and custom HTTPS sources need separate review.

Image construction has different network requirements from guest validation: base-image pulls, OS packages and engine archives belong to the trusted builder policy. Terraform 1.16.2 came from `releases.hashicorp.com`; OpenTofu 1.12.6 came from GitHub and redirected to release assets. The launcher disables checkpoint/update checks, disables backend initialization and performs only init, validate and fmt, so Azure authentication and resource-management endpoints are not dependencies of the validation contract.

## AVM catalog scale

A catalog probe enumerated the Azure namespace, selected the newest release of every `avm-res-`, `avm-ptn-` and `avm-utl-` module, followed nested constraints and measured the resolved codeload archives and providers without running Terraform.

| Catalog fact | Measurement |
| --- | --- |
| AVM roots | 165: 118 resource, 35 pattern and 12 utility modules |
| Resolved module packages | 234, with no dependencies outside AVM |
| Newest release dates | oldest 2024-04-02, median 2026-02-12, newest 2026-09-15 |
| Module files | 62 MiB |
| Provider packages | 15 addresses at 27 versions; ZIPs total 562 MiB |
| Provider mirror unpacked | 2.52 GiB in 52 files |
| Largest expanded tree | 145 manifest records; the preparer's current limit is 256 |
| Largest package directory count | 16 module directories |
| Largest single provider ZIP | azurerm 4.81.0 at 59.1 MiB |

Nested calls usually pin exact versions: 227 of 232 observed edges did so. Thirty-two module sources are needed at more than one version; `avm-res-network-virtualnetwork` is needed at 11, `avm-utl-interfaces` at six, and three modules at five versions each. Thirteen calls in five roots use registry subdirectories such as `.../azurerm//modules/subnet`. The largest root, `avm-ptn-aiml-landing-zone`, expands to 27 packages.

The current preparer accepted 208 of 234 packages. The refusals were 11 parser failures, six selected-file path failures, three duplicate-version cases, two archive collisions, one archive expansion limit, one override-file case and one registry-subdirectory case; some entries appear in more than one category. Six parser failures were only CRLF normalization issues. The remaining parser failures involved an attribute named `in` and heredocs that `python-hcl2` cannot tokenize. One package combined an override file with a `.tofu` file. Concurrent parsing also produced incorrect provider refusals, so a generator must parse serially unless that library is replaced.

Run independently, 139 of 165 roots clear the current refusal rules. The blocked roots need refused packages, multiple versions of one module source, subdirectory calls or trees over 16 packages. No root exceeded the 256 MiB single-root download limit, and the largest root needed 140 MiB of providers. Two preparer defects were found during the catalog measurement: comment keys from `python-hcl2` were mistaken for providers, and concurrent parser use was not safe.

The provider inventory includes six azurerm versions (`3.116.0`, `3.117.1`, `4.81.0`, `5.0.1`, `5.1.0`, `5.5.0`), three azapi versions (`1.14.0`, `1.15.0`, `2.12.0`), three azuread versions (`2.50.0`, `3.4.0`, `3.9.0`), three modtm versions (`0.3.2`, `0.3.5`, `0.4.0`), two time versions (`0.13.1`, `0.14.2`) and one version each of alz, azuredevops, github, pkcs12, ephemeraltls, assert, local, null, random and tls. `chilicat/pkcs12` and `lonegunmanb/ephemeraltls` are community providers requiring separate review. No single version satisfies every module for azurerm, azapi, azuread or time.

### Catalog image options

| Option | Benefit | Cost |
| --- | --- | --- |
| One catalog image | Any AVM root uses one image setting | One change rebuilds everything; approximately 1–3 GiB per sandbox boot |
| Family images | Smaller images and rebuilds | Host chooses a family and dependencies overlap |
| One image per root | Exact dependency set | 165 images and disk imports; impractical on ACAS |

The recommended starting point is one catalog image with a generated, reviewed manifest and an unpacked provider mirror, subject to ACAS import and boot measurements. If a roughly 3 GiB image is too slow, split by family.

The catalog implementation order is: make the module reader match Terraform and apply file checks only to baked files; support multiple versions per source and subdirectory edges; add the reviewed manifest generator and larger limits; unpack providers at build time; run offline init for all 165 roots; then refresh on a schedule with a reviewed manifest diff. The catalog image should be refreshed by a scheduled workflow, with each revision producing a new immutable image and ACAS disk image; old images remain available for rollback.

## Provider linking instead of per-call copying

The AVM measurements showed that the provider ZIP mirror is the dominant image and per-call cost. Terraform's filesystem mirror accepts either ZIPs or unpacked directories at `HOST/NAMESPACE/TYPE/VERSION/TARGET/`. The unpacked form lets Terraform link the provider into the call's data directory instead of copying its bytes, provided the executable bit is preserved.

Terraform 1.16.2 verifies lock hashes, removes the target path and attempts an absolute symlink. If symlink creation fails, it silently creates a directory and recursively copies the package while reporting successful installation. The launcher must inspect `TF_DATA_DIR/providers` after init, require every provider entry to be a symlink into the image mirror and reject any regular file as incomplete. Linking does not avoid reading: Terraform hashes each selected package for the lock file.

A `zh:` lock hash describes the original ZIP and cannot be checked against an unpacked directory; a lock containing only `zh:` hashes is therefore treated as having no usable hashes for this installation path. `h1:` hashes continue to be checked. Supplied locks for an unpacked mirror must carry verifiable `h1:` entries. A plugin cache does not solve this: missing packages are installed into the writable cache, and since Terraform 1.4 a cache entry is used only when the lock already contains a matching checksum. `dev_overrides` skips installation but bypasses version and lock checks, so it is not acceptable for validation.

### Measurements

The Docker measurement used Terraform 1.16.2, four CPUs, a read-only mirror, a fresh container per root and no network. The catalog mirror held 27 provider versions.

| Root or profile | Providers | Cold init | Warm init | Read-only-lock init | Bytes copied | Symlinks | Peak memory |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Typical azurerm 4.x, azapi, modtm, random | 4 | 1.96 s | 0.50 s | 1.05 s | 0 | 4 | 877 MiB |
| Legacy azurerm 3.x, azuread 2.x | 2 | 0.43 s | 0.24 s | 0.44 s | 0 | 2 | 472 MiB |
| Newest azurerm 5.x, azuread, time | 3 | 0.40 s | 0.24 s | 0.41 s | 0 | 3 | 534 MiB |
| Every catalog provider selected by a root | 15 | 0.94 s | 0.65 s | 1.25 s | 0 | 15 | 1102 MiB |
| Unsatisfiable azurerm `~> 6.0` | 0 | 0.09 s | 0.02 s | 0.03 s | 0 | 0 | 143 MiB |

The same two large providers installed from ZIPs took 2.1 seconds and copied 599 MiB into the call, with a 2.18 GB peak. The corresponding unpacked run took about 0.4 seconds and copied zero bytes; validation took about 0.9 seconds in both layouts. Version selection from one mirror was correct: `~> 4.0` chose 4.81.0, `~> 3.116` chose 3.117.1 and `>= 5.0` chose 5.5.0. An unsatisfied constraint failed initialization and installed nothing.

On ACAS, one default-tier sandbox had 1 vCPU, 2.17 GiB memory and a 20 GB root disk, with 296 MB used by the image. Reading a 40 MB provider ZIP from the image took 0.055 seconds at approximately 760 MB/s. The unpacked mirror initialized in 0.71 seconds cold, 0.36 seconds warm and 0.72 seconds with a read-only lock, with zero bytes copied and one link. The guest can drop its page cache, so these cold figures were representative of the guest filesystem. The Docker Desktop cold measurements are softer because host-side cache could not be fully cleared.

The 27-version unpacked mirror occupied 2.52 GiB; gzipped layer size was 549 MiB, compared with 562 MiB of ZIPs. The estimated catalog image was about 2.9 GiB including a 331 MiB base and 62 MiB of modules. Scaling the measured current image ratio suggested an ACAS disk image around 3.4 GB, leaving more than 16 GB on a 20 GB sandbox disk. Import and boot for that larger image were not measured.

### Required build changes and risks

1. Keep fetching and verifying provider ZIPs during trusted preparation, then unpack them during image construction so ZIPs need not remain in a published layer.
2. Record unpacked file digests and the package `h1:` hash in the receipt, and verify that the unpacked tree matches the verified ZIP.
3. Restore executable bits and prove them with `validate`; an init-only probe can pass a non-executable provider and fail later during schema loading.
4. Build from an earlier unpacking stage, verify every file against the receipt and preserve the empty-mirror check.
5. Require the launcher to prove that every provider installation is a symlink into the mirror; otherwise report INCOMPLETE rather than trusting Terraform's silent copy fallback.
6. Change mirror assertions and wrong-lock fixtures from ZIP-only digests to unpacked digests and `h1:` locks.
7. Keep the small profiles for routine CI; build and publish the catalog image on a schedule or by explicit operator action.

The guest can still write into the image mirror when it runs as root, as it can today; disposal removes the call. A read-only bind mount would need privileges unavailable to the sandbox. Reading provider bytes still consumes I/O and page cache. The provider-linking route was measured on Docker and ACAS but is not yet the default image layout.

## Overall limits and follow-ups

- The current validation contract does not support plan, apply, destroy, import, state commands, variable-dependent initialization, arbitrary remote modules, optional policy tools, OpenTofu registry graphs, Windows guests or warm reuse.
- Provider and module compatibility is qualified only for the pinned engine versions, Linux amd64, selected profiles and measured fixtures. Other providers, registries, architectures and backends need independent evidence.
- The catalog does not yet prove that every newest AVM root validates. Only the network virtual network graph at version 0.22.2 was validated offline.
- ACAS import and boot for a 1–3 GiB image, memory use on ACAS, and the 165-root offline build time remain unmeasured. The current estimate is roughly nine minutes for 165 roots at about three seconds per root with ZIPs, not a measured result.
- The Terraform `modules.json` shape for a registry subdirectory call remains to be measured. Override-file semantics, especially the `avm-ptn-alz` package, remain unresolved.
- OpenTofu needs its own mirror, registry graph and compatibility measurements. The current catalog and registry-graph work is Terraform-specific.
- Online dependency access remains a separate, host-controlled profile. If it is revisited, review the complete pinned graph, redirects, request methods, credentials, private-address checks and receiver-side enforcement before changing egress.
- The pinned Terraform distribution carries BSL 1.1 and the pinned OpenTofu distribution carries MPL 2.0. Engine and provider notices must remain separate from the Python package's MIT license.

The decided workload contract is maintained in the [Terraform and OpenTofu kind guide](../kinds/terraform.md), and build, image, preparation and runnable-example instructions are maintained in the [image guide](../../../images/terraform-sandbox/README.md). This record keeps the evidence and unresolved design questions that led to those decisions without duplicating their full operational instructions.
