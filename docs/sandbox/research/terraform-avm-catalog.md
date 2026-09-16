# Baking every latest Azure Verified Module

> Analysis, 2026-09-16, in [#1279](https://github.com/sokolaidev/maf-extensions/pull/1279). It extends the registry module graphs from [#1270](https://github.com/sokolaidev/maf-extensions/issues/1270). Nothing here is implemented.

## Question

Can one prepared image hold the newest release of every Azure Verified Module (AVM) with all its dependencies, so any AVM root validates offline? If so, what must change in the preparer, the image and the launcher?

## Answer

Yes, and the size is manageable. The newest releases of all 165 AVM modules need 234 module packages and 27 provider versions. The module files total 62 MiB. The provider ZIPs total 562 MiB, or 2.52 GiB unpacked.

The #1270 mechanism cannot build that image today. It bakes one version per module source, while AVM modules pin exact versions of each other: `avm-res-network-virtualnetwork` alone is needed at 11 versions. It also has no support for registry subdirectory sources, and `python-hcl2` refuses valid HCL in 11 packages. On top of that, hand-written graphs do not scale to 234 packages that change weekly. Run one at a time, 139 of the 165 modules could each be baked in their own image with the current code.

The recommended route is a generated manifest reviewed as a diff, several versions per module source, subdirectory edges, a module reader that accepts what Terraform accepts, and an unpacked provider mirror. The whole catalog then goes into one image, as long as ACAS import and boot of that image measure acceptable.

## How it was measured

[terraform-avm-catalog-probe.py](terraform-avm-catalog-probe.py) lists the `Azure` namespace on `registry.terraform.io`, keeps `avm-res-`, `avm-ptn-` and `avm-utl-` modules, and takes each module's newest release. It then walks the dependency tree the way registry resolution does: each nested call gets the newest release its constraint admits. It downloads each package's codeload archive at the commit the registry resolves. Each package is parsed with the preparer's own helpers and run through the current preparer (`registry_module_files`) with its resolved dependencies. Providers are resolved per root, over every constraint in that root's tree, and every needed provider package is downloaded and measured. It runs no Terraform or provider code. The results are in [terraform-avm-catalog-evidence.json](terraform-avm-catalog-evidence.json).

The per-call provider cost below was measured separately, with Terraform 1.16.2 in the builtin image under `--network none` on Docker Desktop with 4 CPUs.

## Measurements

### Catalog

| Fact | Value |
|---|---|
| AVM modules | 165: 118 resource, 35 pattern, 12 utility |
| With a release | 165 |
| Newest release published in the last 30 days | 27 |
| Oldest, median, newest release date | 2024-04-02, 2026-02-12, 2026-09-15 |
| Registry location | every version resolves to `git::https://github.com/Azure/...?ref=<commit>` |
| Commits with a verified signature | 232 of 234 |
| Module dependencies outside AVM | none |

### Dependency tree

The 165 roots need 234 packages. 116 roots need no other module and 32 need one more. The largest tree is `avm-ptn-aiml-landing-zone` with 27 packages.

Nested registry calls almost always pin an exact version: 227 of 232, with 5 ranges. So the newest root pulls older dependencies, and 32 module sources are needed at more than one version:

| Module | Versions needed |
|---|---|
| `avm-res-network-virtualnetwork` | 11 |
| `avm-utl-interfaces` | 6 |
| `avm-res-managedidentity-userassignedidentity`, `avm-res-network-privatednszone`, `avm-res-storage-storageaccount` | 5 each |
| 27 other modules | 2 to 4 |

Three packages call one module at two versions themselves. `avm-ptn-alz-connectivity-hub-and-spoke-vnet` 0.17.5 calls `avm-res-network-routetable` 0.3.1 and 0.5.0. `avm-ptn-alz-sub-vending` 0.3.2 calls `avm-res-network-virtualnetwork` 0.14.1 and 0.20.0. `avm-ptn-odaa` 0.1.0 calls it at 0.1.4 and `~> 0.4.0`.

13 calls in 5 roots use a registry subdirectory, such as `Azure/avm-res-network-virtualnetwork/azurerm//modules/subnet`. One of them enters `avm-res-network-firewallpolicy` 0.3.3 at a directory its own root never calls.

The biggest expanded tree below one root call is 145 manifest records, under the preparer's limit of 256. No package has more than 16 module directories.

### The preparer as it is

208 of 234 packages pass `registry_module_files`.

| Refusal | Packages | Cause |
|---|---|---|
| `python-hcl2` parse error | 11 | 6 are CRLF line endings and parse once normalized. 5 still fail: an attribute named `in` (`avm-ptn-policyassignment`), and heredocs `python-hcl2` cannot tokenize (`avm-res-eventhub-namespace`, `avm-res-network-networkinterface`, `avm-res-resources-resourcegroup` 0.2.0, `avm-res-servicebus-namespace`) |
| `file-path` | 6 | names outside the baked directories: `Microsoft Aptos Fonts EULA.rtf` under `examples/`, `~$ure-Migrate-Terraform-User-Guide.docx` under `docs/`, `checker copy.md` |
| `two-versions-of-one-module` | 3 | the three packages above |
| `archive-collision` | 2 | case-only duplicates outside the baked directories, such as two spellings of a pull request template |
| `archive-expansion` | 1 | `avm-res-automation-automationaccount` 0.2.0 expands to 27 MiB; its baked directories alone hold 27 MiB in 35 files |
| `module-override` | 1 | `avm-ptn-alz` 0.21.0 ships an override file and a `.tofu` file; it also uses an attribute named `in` |
| `module-remote` | 1 | a registry subdirectory call |
| `module-unreachable` | 1 | the subdirectory entry described above |

Run as its own image, a root is blocked when any package in its tree is refused, or when the tree needs a subdirectory call, two versions of one module, more than 16 packages or more than 256 MiB of downloads. 139 of 165 roots clear all of that. The 26 that do not are blocked by refused packages (23 roots count at least one), two versions of one module (11), subdirectory calls (5) and trees over 16 packages (4). No root exceeds 256 MiB alone; the largest needs 140 MiB of providers.

Two defects surfaced while measuring. `python-hcl2` adds `__comments__` and `__inline_comments__` keys next to provider requirements, and the preparer read them as providers named `__comments__`; this PR fixes that. `python-hcl2` also gave wrong results under concurrency: when the probe parsed archives from a thread pool, 213 packages were refused on provider grounds, and parsing the same archives on one thread refused none. The preparer parses on one thread, so it is unaffected, but a generator must not parse concurrently.

### Providers

The catalog needs 15 provider addresses at 27 versions.

| Provider | Versions | Notes |
|---|---|---|
| `hashicorp/azurerm` | 3.116.0, 3.117.1, 4.81.0, 5.0.1, 5.1.0, 5.5.0 | 109 roots resolve to 4.81.0, 5 to 5.5.0, 4 to a 3.x release |
| `azure/azapi` | 1.14.0, 1.15.0, 2.12.0 | 2.12.0 unpacks to 366 MiB, the largest package |
| `hashicorp/azuread` | 2.50.0, 3.4.0, 3.9.0 | |
| `azure/modtm` | 0.3.2, 0.3.5, 0.4.0 | |
| `hashicorp/time` | 0.13.1, 0.14.2 | |
| `azure/alz`, `microsoft/azuredevops`, `integrations/github`, `chilicat/pkcs12`, `lonegunmanb/ephemeraltls`, `hashicorp/assert`, `local`, `null`, `random`, `tls` | one each | `chilicat/pkcs12` and `lonegunmanb/ephemeraltls` are community providers that need their own review |

No single version satisfies every module for azurerm, azapi, azuread or time. The mirror must hold several versions, and Terraform picks one per root from what is there. Every ZIP is under the 64 MiB per-download limit; the largest is azurerm 4.81.0 at 59.1 MiB. Every digest matched the registry's download metadata. Packages come from `releases.hashicorp.com` and GitHub release assets.

### Per-call provider cost

Each call unpacks the providers it needs into its own data directory. A root using azurerm 4.81.0 and azapi 2.12.0, three runs each:

| Mirror layout | `init` | Copied into the call | `validate` | Peak memory, page cache included |
|---|---|---|---|---|
| ZIPs, as today | 2.1 s | 599 MiB | 0.9 s | 2.18 GB |
| Unpacked directories | 0.4 s | 0 bytes; Terraform links them | 0.9 s | 0.79 GB |

The ACAS default sandbox tier has 1 core, 2 GB of memory and 20 GB of disk ([Sandboxes overview](https://learn.microsoft.com/azure/container-apps/sandboxes-overview#resource-tiers)). With ZIPs, a call that needs the two large providers comes close to that memory.

### Image size

| Image | Size |
|---|---|
| Builtin base | 331 MiB |
| #1270 image, one AVM module | 435 MiB |
| Catalog, ZIP mirror (estimate) | about 955 MiB: base, 62 MiB of modules, 562 MiB of provider ZIPs |
| Catalog, unpacked mirror (estimate) | about 2.9 GiB: base, modules, 2.52 GiB of providers |

The Sandboxes documentation gives no size limit for an imported disk image. The #1270 image took 19 s to import. Import and boot times for a 1 to 3 GiB image are not measured.

## What has to change

1. **Generate the manifest.** A host tool resolves newest releases and nested pins from the registry, pins each commit and archive digest, derives each graph, resolves providers, and writes the manifest. People review the diff. The preparer still verifies everything independently. This changes the trust model: pins are taken from the registry when the manifest is generated, and review moves from each module's source to the change between two catalogs. The commit signature status can go into provenance, but it proves who committed, not what was reviewed.
2. **Several versions per module source.** A package is identified by source and version. Each edge names a package, which also covers a package calling one module at two versions. For an authored call, the launcher picks the newest baked version the constraint admits, using go-version rules. Terraform re-checks the record, so a wrong pick is incomplete, never a pass. Nested records keep coming from the inventory.
3. **Registry subdirectory sources.** Accept `source//dir` edges and let a package have several entry directories, with reachability counted from all of them. The `modules.json` record shape Terraform writes for such a call must be measured first.
4. **A module reader that matches Terraform.** Normalizing CRLF fixes 6 of the 11 parse failures and leaves 5. Use the launcher's block reader for module edges in the preparer too, so both sides read calls the same way and it handles heredocs and comments. Take provider completeness from the build-time offline `init`, which already checks provider constraints against the mirror, instead of parsing `required_providers` in Python.
5. **Check only what is baked.** Every `file-path` and `archive-collision` refusal, and most of the archive text limit, comes from files the image never bakes. Keep the ZIP container checks for every entry, but apply name, collision and text rules to the selected files, and bound the selected bytes per package.
6. **Raise the limits.** 16 registry modules per manifest becomes a few hundred; 256 MiB of downloads becomes about 1 GiB, since the catalog is 635 MiB of ZIPs and archives. The 180-second deadline for all downloads needs measuring at that volume.
7. **Unpack providers at build time.** Verify each unpacked directory's `h1:` hash against the ZIP it came from. Calls then link providers instead of copying them. [One image with every provider](terraform-provider-linking.md) measures that route on Docker and on ACAS.
8. **Overrides.** One package, `avm-ptn-alz`, ships an override file. Either implement Terraform's override merge when deriving its graph, or keep refusing that module.
9. **Prove the catalog at build time.** Run offline `init` once per root, 165 times. At about 3 s per root with ZIPs, that is roughly 9 minutes, and less with an unpacked mirror. This is not measured.
10. **Refresh on a schedule.** A scheduled workflow regenerates the manifest and opens a pull request with the diff, like the lock refresh. Each merge produces a new image revision and a new ACAS disk image; older disk images stay for rollback.

## Packaging options

| Option | For | Against |
|---|---|---|
| One catalog image | any AVM root works with one image setting | 1 to 3 GiB per sandbox boot; one change rebuilds everything |
| Family images, such as networking, compute and landing zones | smaller images and rebuilds | the host picks the image per workload; families share dependencies |
| One image per root | exact | 165 images and disk imports; not practical on ACAS |

Start with one catalog image with an unpacked mirror, and measure ACAS import and boot before committing to it. If a 3 GiB disk image is too slow, split by family.

## Suggested order

1. The module reader and baked-only file checks. This unlocks 20 of the 26 refused packages, provided the per-package bound admits the 27 MiB automation account module.
2. Several versions per source, and subdirectory edges. This unlocks the other refusals except `avm-ptn-alz`, which needs override handling.
3. The manifest generator and higher limits.
4. The unpacked provider mirror.
5. The catalog image build, with ACAS import and boot measured.
6. The scheduled refresh.

## Not measured

- ACAS import and boot time for a 1 to 3 GiB disk image, and memory use on ACAS.
- The `modules.json` record Terraform writes for a registry subdirectory call.
- Build-probe time over 165 roots.
- Whether every newest root validates. Only `avm-res-network-virtualnetwork` 0.22.2 has validated offline, in #1270.
- Resolution through `registry.opentofu.org`.
- The probe's version-constraint evaluator beyond its own results. It follows go-version for modules and go-versions for providers, and reported no constraint it could not evaluate.
