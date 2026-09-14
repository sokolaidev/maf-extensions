# Terraform and OpenTofu dependency egress

Investigation for [#1246](https://github.com/sokolaidev/maf-extensions/issues/1246), observed 2026-09-14. The [implemented validation kind](../kinds/terraform.md) needs **no runtime egress**: engines and selected providers are in the image, modules are staged locally, and the sandbox uses `CLOSED`. This report identifies hosts for a future online dependency installation profile.

## Hosts by engine

These are HTTPS destination names on port 443, using each engine's default public registry. The provider scope is `hashicorp/random` 3.7.2, `hashicorp/azurerm` 4.0.0, `hashicorp/azuread` 3.0.2 and `Azure/azapi` 2.0.1, Linux amd64. They are measured fixtures, not a claim about every provider or version.

| Engine and dependency scope | Required destination hosts |
|---|---|
| Terraform: the three sampled HashiCorp providers | `registry.terraform.io`, `releases.hashicorp.com` |
| Terraform: those providers plus AzAPI | `registry.terraform.io`, `releases.hashicorp.com`, `github.com`, `release-assets.githubusercontent.com` |
| OpenTofu: all four sampled providers | `registry.opentofu.org`, `github.com`, `release-assets.githubusercontent.com` |
| Either engine: GitHub module archive URLs | Add `github.com` and `codeload.github.com` |
| Either engine: GitHub HTTPS Git module sources | Add `github.com`; registry-addressed modules also need their module registry |

The registries supply service discovery, version information and package metadata. Package archives, SHA-256 manifests and detached signatures can live elsewhere: the [Terraform provider protocol](https://developer.hashicorp.com/terraform/internals/provider-registry-protocol) and [OpenTofu provider protocol](https://opentofu.org/docs/internals/provider-registry-protocol/) explicitly return those download URLs. The sampled HashiCorp providers use `releases.hashicorp.com` through Terraform and GitHub releases through OpenTofu. AzAPI uses GitHub releases through both registries. GitHub redirects all sampled release assets to `release-assets.githubusercontent.com`, so allowing only `github.com` would miss the final download host.

Explicit provider addresses can select a different registry even when the engine stays the same. Third-party providers can name other artifact hosts; private registries, configured network mirrors and module dependencies can change the set. An engine name alone is insufficient to derive a universal allowlist. Resolve and review the complete pinned dependency graph for the intended profile, including redirect destinations.

## Modules and HTTP methods

Both registries resolved `Azure/avm-res-resources-resourcegroup/azurerm` 0.2.0 to the public GitHub repository at commit `7a11372c80143bcf3566fe6daedafc25d7f7477f`. Terraform returned HTTP 204 with `X-Terraform-Get`; OpenTofu returned HTTP 200 with a JSON `location`, as supported by its [module registry protocol](https://opentofu.org/docs/internals/module-registry-protocol/).

The returned source is `git::https://github.com/...`, which installs through Git. The [Terraform module documentation](https://developer.hashicorp.com/terraform/language/modules/configuration) and [OpenTofu source documentation](https://opentofu.org/docs/language/modules/sources/) describe that transport. The probe separately requested a GitHub archive for the same commit and observed its redirect to `codeload.github.com`. That establishes the archive route; it does not establish that the registry's Git source needs `codeload.github.com`.

Provider and archive probes use GET. A method-filtering proxy must also accommodate Git's HTTPS reference discovery and POST to `git-upload-pack` for Git sources; GET/HEAD alone is insufficient. See the [Git HTTP protocol](https://git-scm.com/docs/http-protocol). An online image supporting Git sources also needs the Git executable. Nested modules and Git submodules need their own source-host review. S3, GCS, OCI, GitLab, Bitbucket and custom HTTPS sources have additional source-specific requirements outside these fixtures.

## Image construction and validation

The current image installer downloads Terraform 1.16.2 from `releases.hashicorp.com`. OpenTofu 1.12.6 downloads from `github.com`, redirecting to `release-assets.githubusercontent.com`. Installing the pinned provider mirror adds its artifact hosts above, but no registry lookup when the installer already has the pinned URL and digest. Container base-image pulls and any OS package installation belong to the builder's network policy and are outside this guest dependency-host list.

The launcher disables checkpoint/update checks with `CHECKPOINT_DISABLE=1`. No checkpoint endpoint is needed for this profile. Backend initialization is disabled, and the kind performs initialization, validation and formatting only. Azure authentication and resource-management endpoints are therefore not dependencies of this validation contract; [Terraform validation](https://developer.hashicorp.com/terraform/cli/commands/validate) does not validate remote services or backend APIs. These metadata probes do not prove that arbitrary provider code never attempts network access.

Opening the listed hosts alone would not enable online provider installation in the current implementation. Its controlled CLI configuration contains only `filesystem_mirror`, with no `direct` fallback. A future online profile needs explicit installation configuration, approved dependencies and adapter verification as well as an egress policy. This investigation changes no runtime policy.

## Evidence and reproduction

The [probe](terraform-egress-probe.py) and [sanitized evidence](terraform-egress-evidence.json) record public metadata and HTTPS redirect hosts. All eight provider metadata requests succeeded, as did all 24 package/checksum/signature URL probes, the two module lookups, the two independently requested module archives and the two engine archive probes. Discovery documents kept both services on each registry's own host.

```text
python docs/sandbox/research/terraform-egress-probe.py --output docs/sandbox/research/terraform-egress-evidence.json
```

The standard-library probe reads bounded metadata and at most one byte from each artifact response. It records status, public path and redirect hostnames, omitting signed asset query strings. It uses a research User-Agent and does not run either CLI or any provider, download and verify complete archives, clone modules, or exercise a sandbox proxy. The record establishes observed endpoint routes, not successful initialization through an enforced allowlist. Recheck the selected versions and their dependency graph before implementing such a profile; download destinations can change.
