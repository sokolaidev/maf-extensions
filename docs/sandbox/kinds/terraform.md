# Terraform and OpenTofu

[`maf-sandbox-terraform`](../../../packages/maf-sandbox-terraform/README.md) adds two selectable workloads alongside Bicep. The host selects `engine="terraform"` or `engine="opentofu"` once, attaching `terraform_validate` or `opentofu_validate`. Each tool receives an explicit file manifest and root module, validates it offline, and checks formatting without modifying the store.

| Contract | Behavior |
|---|---|
| Capabilities | `EXEC`, `FILES_IN` |
| Guest family | `POSIX`, backed initially by pinned Linux amd64 images |
| Isolation and cleanup | At least container isolation, `CALL` scope, `DISPOSE` floor |
| Egress | `CLOSED`, fixed filesystem provider mirror without direct fallback |
| Dependency lock | Supplied root lock read-only; absent lock generated only in disposable guest |
| Reports | Initialization incomplete vs validation pass/fail; separate formatting verdict |
| Information flow | Untrusted derived report plus fixed standing guidance; hidden names suppress guest prose |

All files are read through the core session with their original listing records, checked against transfer ceilings, and staged before the fixed launcher runs. The tool refuses missing files, normalized name collisions, omitted configuration siblings in selected directories, reserved paths, and roots without a recognized configuration file. Dot-prefixed configuration files are refused because the engines ignore them. Terraform mode also refuses `.tofu` files; OpenTofu retains its native `.tofu` precedence. The original [investigation](../research/terraform-kind.md) records the CLI evidence and design alternatives.

Initialization uses `-backend=false` and noninteractive mode. A failed dependency initialization never becomes a valid empty report. `validate -json` requires a supported 1.x output format, boolean verdict, consistent diagnostic counts, and matching process exit status. Malformed, truncated, oversized, or inconsistent output is incomplete validation. Formatting runs independently with `fmt -check -recursive` across the staged project.

The immutable launcher constructs an environment without inherited CLI arguments, credentials, variables, logging settings, or provider overrides. Its private data directory is outside the project tree. It supervises each CLI process group, drains both pipes under one 128 KiB output ceiling, and shares the configured deadline across CLI phases. The host waits through cancellation until execution finishes within its bound; the core then disposes the entire sandbox. This is a disposal contract: providers and expressions can access paths outside the staged module, so there is no call-directory confinement or warm-reuse claim.

Build and runnable examples are in [the image guide](../../../images/terraform-sandbox/README.md). The built-in/local-module and `random` provider profiles pin their engine, provider archive, and guest platform. Other providers and backends require their own qualification. No numbered sample advertises an unpublished package version.

## Status

| Work | State | Tracker |
|---|---|---|
| Offline Terraform/OpenTofu validation, fixed launcher, images, examples, tests, and package registration | implemented locally; package not yet released | [#1246](https://github.com/sokolaidev/maf-extensions/issues/1246) (open) |
| Online dependency access restricted to approved artifacts and request paths | planned follow-up; validation retains closed egress | [#1249](https://github.com/sokolaidev/maf-extensions/issues/1249) (open) |
| Plan/apply/state commands, variable-dependent initialization, optional policy tools, and warm reuse | outside the first-version scope | scope recorded in [#1246](https://github.com/sokolaidev/maf-extensions/issues/1246) (open) |

The live suite measures actual Docker adapter calls for both engines, including local modules, JSON input, schema errors, unavailable dependencies, formatting, wrong-engine images, cancellation, timeouts, and daemon-observed disposal. The launcher suite independently exercises environment construction, shared output/time bounds, inherited pipes, read-only supplied locks, and source/state nonmutation. Local execution results belong to the implementation's issue record; adding the workflow does not establish that remote CI has run. ACAS and WSLC have not been measured for this workload.
