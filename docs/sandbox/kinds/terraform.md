# Terraform and OpenTofu

The host selects `engine="terraform"` or `engine="opentofu"`. The kind validates a supplied project offline and checks formatting. Optional formatting tools return changed file contents for the model to save through host file tools.

See the [package README](../../../packages/maf-sandbox-terraform/README.md) for wiring, and the [image usage guide](../../../images/terraform-sandbox/USAGE.md) for runnable examples.

## Contract

| Setting | Behavior |
|---|---|
| Kind | `terraform` or `opentofu`, selected by the host |
| Validation tool | `terraform_validate` or `opentofu_validate` |
| Optional formatting tool | `terraform_format` or `opentofu_format` |
| Required capabilities | `EXEC`, `FILES_IN` |
| Guest | POSIX; supplied images pin Linux amd64 engines and dependencies |
| Isolation and lifetime | At least container isolation; a separate sandbox per call; mandatory disposal |
| Network | `CLOSED`, with a filesystem provider mirror and no direct-download fallback |
| Results | Untrusted report followed by fixed trusted guidance |

This kind exposes no plan, apply or state commands. It does not write to the agent's store. Providers and expressions may access other guest paths, so there is no call-directory confinement or warm-reuse claim.

## Inputs and validation

Each call supplies a file manifest and root module. All files are read through the session with their original listing records, checked against transfer limits and staged before execution.

The tool refuses:

- Missing files, normalized name collisions and reserved paths.
- Configuration siblings omitted from a selected directory.
- Roots without a recognized configuration file.
- Dot-prefixed configuration files, which the engines ignore.
- `.tofu` files in Terraform mode. OpenTofu keeps its native precedence rules.

The fixed launcher runs noninteractive initialization with `-backend=false`. A supplied root lock is read-only. Without one, any generated lock exists only in the disposable guest.

Initialization failure means incomplete validation. `validate -json` must return a supported 1.x format, a Boolean verdict, consistent diagnostic counts and a matching exit status. Malformed, truncated, oversized or inconsistent output is incomplete.

Formatting checks use `fmt -check -recursive` across the staged project. Validation returns no formatted file contents; the model edits the files through host tools.

## Optional formatting

Set `formatting=True` to attach a separate formatting tool. Its separate name lets the host expose or approve it independently. Validation-only is the default.

The formatting tool accepts the same manifest and root. It runs `fmt -recursive -no-color` without dependency initialization or validation. A base image with no providers is sufficient.

The result is a JSON mapping from store-relative paths to whole changed files. Unchanged files are omitted. Every returned path must belong to the staged manifest.

| Bound | Behavior |
|---|---|
| Complete report | At most 128 KiB, including JSON escaping and metadata |
| CLI output plus returned file bytes | Shared 128 KiB allowance |
| Failure or overflow | No partial formatted text |
| Hidden argument names | Formatted file contents are withheld |

An oversized project needs a smaller complete manifest. A single changed file larger than the bound cannot be returned. Saving formatted text remains a separate host-tool call with the host's approvals.

## Result labels and tool flow

Provider programs and stored configuration are sources of the reports. Formatted file contents also come from that configuration. The kind claims `untrusted` for both validation and formatting output.

![Terraform and OpenTofu validation or formatting tools return an untrusted report and fixed trusted guidance as separate content items. The wrapper's framework declaration is trusted, while the workload claim remains untrusted. Items retain the call's effective confidentiality. FIDES shows text or a hidden reference to the model. A later file-write tool can persist formatted files only after the host's integrity, confidentiality and approval checks. The validation and formatting tools themselves do not write to the host store.](../assets/terraform-information-flow.svg)

The wrapper raises the framework-facing declaration to keep guidance readable. It stores the workload claim in `maf_sandbox_derived_integrity` and labels the report separately. Hidden names suppress guest prose in reports.

A hidden report is not evidence of success. Hidden content still affects confidentiality, and forwarding its reference remains subject to host policy. The host chooses result classification. See [information flow](../information-flow.md).

## Execution limits and cleanup

The launcher removes inherited CLI arguments, credentials, variables, logging settings and provider overrides. Its private data directory is outside the project tree.

Each CLI process group is supervised. Both output streams share one 128 KiB ceiling, and all phases share the configured deadline. Cancellation waits for bounded execution to finish before core disposes the sandbox.

Disposal is required even after a successful check. Keeping only the staged directory clean would not account for provider activity elsewhere in the guest.

## Images and prepared dependencies

The [image guide](../../../images/terraform-sandbox/README.md) owns engine pins, provider profiles and build commands. Select an image that contains the dependencies the project needs; other profiles and backends need their own validation.

Dependency preparation is a host-controlled image-build step. It downloads only approved, pinned artifacts, verifies their content and writes a provider mirror, module files and a sanitized receipt. It runs no provider executable on the host.

| Dependency | Supported preparation |
|---|---|
| Providers | Full identity, version, platform, digest and source reference are pinned |
| Multiple provider versions | Policy may approve several version ranges; duplicate resolved versions are refused |
| Local module bundles | A complete graph is included in the image |
| Registry modules, including Azure Verified Modules | Terraform only; pinned graphs and catalog selections are baked into the image |
| OpenTofu registry modules | Not provided by preparation |

Only preparation has network access. Validation stays offline and gives the model no download interface. Missing modules leave initialization incomplete; other remote or dynamic module sources are refused.

Registry module sources and version constraints remain as authored. Prepared providers are unpacked and linked into each call. The launcher refuses copied providers and permits OpenTofu's empty lock beside a link. Supplied `h1:` hashes are checked; `zh:`-only locks are not checked against the unpacked mirror.

Prepared receipts pin the launcher as `reader_sha256`. Rebuild both base and derived images when changing launcher modes; replacing only the launcher file does not update that receipt.

The [platform image](../../../images/terraform-sandbox/README.md#azure-platform-provider-image) adds service-specific OpenTofu providers. The [multi-version example](../../../images/terraform-sandbox/USAGE.md#build-an-image-with-two-provider-lines) shows two provider lines in one offline mirror.

## Status

| Contract | State | Details |
|---|---|---|
| Offline validation and optional formatting | Implemented | [Package README](../../../packages/maf-sandbox-terraform/README.md) |
| Approved providers, local modules and Terraform registry modules | Implemented; support depends on the selected image | [Image guide](../../../images/terraform-sandbox/README.md) |
| Plan, apply, state operations and warm reuse | Outside the supported contract | [Package README](../../../packages/maf-sandbox-terraform/README.md) |
| Four-field result contract | Open; tools return report and guidance items | [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (open) |
