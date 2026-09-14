# A Terraform sibling for Bicep, with Terraform and OpenTofu engines

> Investigation, 2026-09-14. Repository baseline: [`93c94d9c2a8bac82c9c72dc5bff812d2a3a1d372`](https://github.com/sokolaidev/maf-extensions/tree/93c94d9c2a8bac82c9c72dc5bff812d2a3a1d372). This proposes a workload package and records direct CLI measurements. No Terraform workload package or framework integration is implemented by this research.

## Recommendation

Add **`maf-sandbox-terraform`**, importing as `maf_sandbox_terraform`, alongside `maf-sandbox-bicep`. Give its host factory an `engine` option accepting exactly **`"terraform"` and `"opentofu"`**, defaulting to `"terraform"`. Both use the same staging, execution and reporting implementation, with explicit engine metadata. The initial workload validates configuration and checks formatting. Planning, applying, destroying, importing, state commands and `test` are separate work: Bicep's sibling is a validation tool, and those operations introduce different authority and lifecycle requirements.

The first release should run with closed egress, host-controlled provider packages, and a new sandbox for each call. This is feasible on the current protocol without a new backend API. The live probe establishes that both pinned CLIs can do useful validation under these constraints; it does not establish framework integration or universal provider compatibility.

Repository source search and GitHub issue/PR searches for `terraform` and `opentofu` found no existing implementation or tracking on the investigation date.

## What transfers from Bicep

[`_tool.py`](../../../packages/maf-sandbox-bicep/src/maf_sandbox_bicep/_tool.py) supplies the useful pattern: the host binds identity and configuration at attach, the model names files, the kind reads through `SandboxToolSession`, and core owns admission and cleanup. The kind needs `EXEC` and `FILES_IN`; returning diagnostics through exec output needs no `FILES_OUT`. Follow the current [kind-writing guide](../kinds/writing-a-kind.md), including `work_dir=None`, relative call paths, argv sequences and file provenance. Do not import Bicep's private path or SARIF modules into the sibling.

Three things need their own design. Terraform validates a directory and its module graph, rather than compiling each named file. Initialization installs dependencies before validation. Providers are executable programs: HashiCorp documents schema and configuration-validation RPCs during `validate`, and the probe observed provider processes starting on both engines. Bicep's fixed Microsoft-host allowlist and its confinement declaration therefore cannot simply be copied. [Provider RPC reference](https://developer.hashicorp.com/terraform/plugin/framework/internals/rpcs).

## Proposed public contract

The following is an API proposal, not an importable example:

```text
TerraformEngine = Literal["terraform", "opentofu"]

terraform_sandbox_spec(image=None, image_id=None, *, engine="terraform")
make_terraform_tools(router, file_store, agent_id, context, *,
                     engine="terraform", image=None, image_id=None,
                     exec_timeout_seconds=120, file_store_provenance=None)

selected_validate(files: list[str], root_module: str = ".")
```

| Host option | Guest executable | Spec kind | Exposed tool |
|---|---|---|---|
| `terraform` | `terraform` | `terraform` | `terraform_validate` |
| `opentofu` | `tofu` | `opentofu` | `opentofu_validate` |

Reject unknown option values at construction, including `"tofu"`; that is the executable name, not a third engine. Select the engine at attach, not in model-controlled arguments. Do not fall back to the other binary when the requested one is missing. The report and tool description identify the selected engine and observed version. Separate kind values prevent cross-engine sharing and let a host deliberately attach both tools without duplicate names. This remains one Python package and one implementation.

Use `SandboxSpec(requires={EXEC, FILES_IN}, requires_os_family=POSIX, work_dir=None, egress=CLOSED, min_isolation=CONTAINER, isolation_scope=CALL, min_cleanup=DISPOSE, confined_to_guest_call_path=False)`. The container floor excludes the in-process demonstration backend from executing native providers; a host can raise it. `CALL` separates concurrent requests, including requests served by different routers, and core disposes the instance. It is stronger than exclusive admission alone. This initial design deliberately pays for creation per call. Keep engine binaries, the provider mirror and launcher in the image; no Python wheel needs to bundle them.

Current core also has `SandboxSpec.execution_contract`, used by CodeAct and checked for instances known to a router. It could carry the selected engine and image-profile revision as an additional consistency check; it is not persistent image attestation and does not replace the separate kind values or call scope. The source baseline contains prepared dependent bounds beyond core's packaged version, so choose a published core supporting the final API when implementing, and move dependency floor and ceiling together. This investigation changes no versions.

## Stage a complete input snapshot

`files` is the explicit input manifest from the host's file listing. Preserve relative paths beneath a fresh `project/` directory under `session.guest_call_path()`. `root_module` selects a directory inside that staged tree. A root at `project/root/` may legitimately refer to `../modules/child`: that stays within the staged snapshot. Reject absolute input paths, traversal outside the staged tree, duplicate normalized destinations, and collisions with runner-owned directories before writing. Read each original `ListedFile` through `session.read_file`; abort on any missing, refused, over-limit or failed transfer instead of validating a partial upload.

Require at least one configuration file recognized by the selected engine in the selected root. Check the file listing for omitted configuration siblings in every staged module directory and return an incomplete-input error if one was left out. The result's scope is the supplied root and staged snapshot, never every file in an unrelated repository. Missing local modules remain initialization failures; never replace them with empty stubs.

Terraform accepts `.tf` and `.tf.json` configurations. OpenTofu additionally accepts `.tofu` and `.tofu.json`, with its own precedence rules. Preserve the filenames; do not rename `.tofu` into `.tf` or concatenate files. In Terraform mode reject a manifest containing OpenTofu configuration files rather than silently certifying the part Terraform happened to read. The live `tofu_only` fixture demonstrates why this guard is required. [OpenTofu file selection](https://opentofu.org/docs/language/files/).

Support explicitly listed ancillary text files needed by `file()` and `templatefile()`; a configuration-only extension filter would make normal modules fail. Reserve runner configuration and dependency installation paths. Exclude uploaded state, saved plans, `.terraform/`, user CLI configuration, credential files and plugin executables. Accept `.terraform.lock.hcl` only as dependency metadata. Defer variable-file inputs and variable-dependent module sources from the initial contract; the wrapper must report those unsupported initialization requirements instead of prompting. This is not a general HCL security validator: file path checks do not confine expressions or a running provider to the staged snapshot.

## Execute fixed phases and report their actual scope

The shared command subset is small:

```text
<engine> init -backend=false -input=false -no-color
<engine> validate -json
<engine> fmt -check -recursive -no-color
```

Run initialization and validation from the selected root. Run formatting from the staged project directory so sibling local modules are covered too; place `TF_DATA_DIR` outside that directory so formatting does not walk installed dependencies. `fmt -check` leaves the user's source unchanged. A style mismatch is a separate result from semantic validity, and formatting is not a replacement for Bicep's lint rules. TFLint or policy scanners would need their own configuration, dependencies and claims. [Terraform formatting](https://developer.hashicorp.com/terraform/cli/commands/fmt), [OpenTofu formatting](https://opentofu.org/docs/cli/commands/fmt/).

Both projects document initialized dependencies as a prerequisite for validation and `-backend=false` for skipping backend initialization. A successful validation establishes configuration checks, not a cloud plan or deployment outcome. Stop after failed initialization, and return `INITIALIZATION FAILED — VALIDATION INCOMPLETE`; source syntax failures can occur in that phase too. Do not run `validate` against whatever partial dependency installation remains. [Terraform validation](https://developer.hashicorp.com/terraform/cli/commands/validate), [OpenTofu initialization](https://opentofu.org/docs/cli/commands/init/).

Parse the validation object independently of the process exit code. Require a supported `format_version` major version, correctly typed fields and consistent `valid`/error counts; retain warnings and useful diagnostic locations. Preserve unknown optional fields without depending on them. Treat absent, malformed, oversized or truncated JSON, unsupported format versions, or disagreement with process status as an execution failure. Initialization text is not the validation JSON schema. The CLI can fail before its JSON renderer starts. [OpenTofu validation output](https://opentofu.org/docs/cli/commands/validate/).

Use a fixed image launcher because `Sandbox.exec` has no environment argument. The launcher derives absolute scratch paths from its working directory, selects an allowlisted binary and phase, and builds a clean environment. Keep `HOME`, `TMPDIR`, `TF_DATA_DIR` and any cache within the call; set `TF_CLI_CONFIG_FILE` to the controlled configuration. Exclude inherited `TF_CLI_ARGS*`, `TF_VAR_*`, `TF_TOKEN_*`, logging destinations, provider overrides, cloud credentials and credential helpers. Set noninteractive/automation flags and disable update checks. OpenTofu still uses these `TF_*` controls. [Terraform environment](https://developer.hashicorp.com/terraform/cli/config/environment-variables), [OpenTofu environment](https://opentofu.org/docs/cli/config/environment-variables/).

The production launcher needs bounded output capture and process supervision. Share a finite deadline across its phases; make truncation visible and never turn a truncated report into a pass. Cancellation must drain or terminate the command before core disposes its instance, and failures must retain bounded host diagnostics. The supplied research script captures only fixed fixtures and is not that production launcher.

## Dependencies, egress and lockfiles

Start with an image containing a read-only filesystem mirror of explicitly selected providers and a CLI configuration containing only `filesystem_mirror`, with no `direct` fallback. Built-in `terraform_data` needs no external provider; the live probe also validates an installed `random` provider with networking disabled. A missing mirrored dependency is incomplete validation. A plugin cache alone is not an offline installation policy. Both CLIs document explicit provider installation configuration. [Terraform CLI configuration](https://developer.hashicorp.com/terraform/cli/config/config-file), [OpenTofu CLI configuration](https://opentofu.org/docs/cli/config/config-file/).

A provider mirror does not supply remote module source packages. The initial contract supports staged local modules. Remote module and registry access needs a later dependency profile specifying provider identities, versions, checksums, module sources and required download hosts. There is no universal Terraform equivalent of Bicep's four hosts: provider registries can direct downloads elsewhere, and module sources extend beyond registries. A future allowlist must come from host configuration, never from the model's desired URLs; unrestricted runtime egress should not be an engine fallback. [Module source options](https://developer.hashicorp.com/terraform/language/modules/configuration).

If the snapshot includes a lockfile, initialize with `-lockfile=readonly` and preserve it. If no lockfile exists, let initialization create one only in the disposable guest copy and report the selected provider versions; the host's immutable mirror bounds the candidates. Do not return a generated lockfile to the file store implicitly. A host can require a lockfile as an additional policy. The probe confirms that requiring readonly mode without an existing provider lock fails on both engines, while reinitializing with the generated lock succeeds.

Both engines use `.terraform.lock.hcl`, but default registry addresses differ. For the tested shorthand `hashicorp/random`, OpenTofu attempted to migrate a Terraform lockfile to its own registry address, discarded the old hashes in the proposed migration, and then refused because the lockfile was readonly. Treat that as an actionable dependency mismatch, not a reason to retry without readonly mode. The lockfile tracks provider selections; it does not freeze a whole remote module graph. [OpenTofu dependency locks](https://opentofu.org/docs/language/files/dependency-lock/).

The mirror used here intentionally contains the same HashiCorp `random` archive under both registry addresses to isolate CLI behavior. That does not establish that the public registries distribute identical provider archives, or authorize silently translating addresses in the product. Image profiles must preserve provenance and checksums for the provider bytes they actually ship.

## Containment and information flow

Closed egress prevents runtime downloads and remote API access, but does not make configuration passive. The probe's `file()` expression successfully read a known file outside its call directory; the otherwise identical missing-file case failed. Both engines also started provider processes. These are guest filesystem access and native execution, even though no plan or apply was requested. Keep credentials and unrelated data out of the guest and retain call isolation/disposal. Do not copy `confined_to_guest_call_path=True` from Bicep or advertise warm reuse on the strength of redirected cache paths.

Use `source_integrity=UNTRUSTED` for every call-derived result, including a one-word verdict, initialization output and diagnostic snippets. Preserve the current split-result pattern: the derived report followed by one constant `standing_guidance` item stating that hidden or incomplete results do not establish a pass. Honor hidden input-name handling in summaries, locations and messages; compiler text and source snippets are not trusted instructions. Pass the host's file provenance through instead of reading the store directly. [Information-flow contract](../information-flow.md).

Keep the Python package and engine distributions separate. The pinned Terraform source carries BSL 1.1 and the pinned OpenTofu source carries MPL 2.0; image distribution must retain the relevant engine and provider notices rather than imply the Python package's license covers those binaries. [Terraform license](https://raw.githubusercontent.com/hashicorp/terraform/v1.16.2/LICENSE), [OpenTofu license](https://raw.githubusercontent.com/opentofu/opentofu/v1.12.6/LICENSE).

## Measured compatibility

The [probe](terraform-cli-probe.py) ran Terraform **1.16.2** and OpenTofu **1.12.6**, Linux amd64, with HashiCorp `random` **3.7.2**. Archives matched the publishers' SHA-256 manifests; the reproduction script pins those hashes. Execution used Docker 29.7.2, UID 65534, closed networking, a read-only root filesystem, all capabilities dropped, no-new-privileges, two CPUs, 768 MiB memory, 128 PIDs, and a 384 MiB writable tmpfs. Inputs and binaries were copied into a local research image; no host directories or credentials were mounted. The base image is pinned in the [Dockerfile](terraform-cli-probe.Dockerfile).

| Fixture | Terraform 1.16.2 | OpenTofu 1.12.6 |
|---|---|---|
| Root plus sibling local module, required variable left unset | init 0; valid | init 0; valid |
| `.tf.json` with built-in resource | valid | valid |
| Undeclared variable | validation exit 1; JSON error | validation exit 1; JSON error |
| Syntax error / missing local module / missing mirrored provider | initialization exit 1; validation skipped | same |
| Mirrored `random_string`, valid and wrong attribute type | valid / JSON error; provider processes started | same |
| Readonly initialization without provider lockfile | initialization exit 1 | initialization exit 1 |
| Readonly reinitialization after generating provider lockfile | succeeds | succeeds |
| Empty partial S3 backend with `-backend=false` | valid | valid |
| Only an invalid `.tofu` file | **reports valid: file ignored** | JSON error |
| Invalid `main.tf` plus valid `main.tofu` | JSON error | valid: `.tofu` takes precedence |
| Formatting mismatch with valid semantics | validation 0, fmt 3 | validation 0, fmt 3 |
| `file()` reads known file outside call / missing counterpart | valid / JSON error | valid / JSON error |
| Terraform-generated shorthand-provider lock, OpenTofu readonly init | source of the lock | initialization exit 1; migration required |

There are 31 engine/fixture combinations. Every executed validation returned JSON format `1.0`. Both version commands used the JSON key `terraform_version`; that key alone does not identify the engine. No `*.tfstate*` file was found in any fixture's call tree. The compact [evidence record](terraform-cli-evidence.json) preserves exit codes, verdicts, provider startup observations and dependency addresses.

These measurements cover the named versions and one external provider. They do not establish Azure/AWS provider behavior, remote module installation, a general filesystem/process confinement claim, a supported version range, Windows guest support, router cleanup, cancellation behavior or ACAS/WSLC execution. No infrastructure was planned or deployed, and no cloud credentials were used.

## Reproduce

From the repository root, with PowerShell 7 and a Linux Docker engine:

```powershell
./docs/sandbox/research/terraform-cli-probe.ps1 -OutputDirectory ../terraform-research-evidence
```

The script downloads or verifies the three pinned archives, builds a local image, runs the fixed fixtures without network access and writes `raw-results.json`, `downloads.json` and `image-id.txt` into the requested output directory. It removes the probe container and retains the local image and evidence for inspection. Image preparation can need network access for the pinned base image; fixture execution cannot. This reproducible route avoids Windows bind-mount file sharing.

## Implementation slices and acceptance

1. **Package and tool contract.** Add the self-contained sibling package, the exact engine selector, engine-specific names, session-based input staging and split results. Register it with workspace, release, package metadata and dependency checks following existing packages. Pin construction refusals, both engine mappings, missing tools, provenance, hidden names, omitted siblings and partial-upload refusal with focused tests. Keep existing Bicep behavior unchanged.
2. **Runtime and dependency profile.** Add separately pinned image targets and a fixed launcher with clean environment, mirror-only provider installation, lockfile handling, bounded output and deadline/cancellation supervision. Exercise both engines with the fixtures above, checksum mismatch, blocked remote modules, CLI override attempts and malicious or oversized diagnostics. A missing dependency must never yield a successful verdict. Neither image should require ambient cloud identity.
3. **Framework proof and samples.** Drive both tools through a real Docker backend, asserting call scope, daemon-observed disposal on success/error/cancellation, different instance IDs on successive calls, and no cross-engine sharing. Add a minimal built-in/local-module example and a mirrored-provider example. Resolve sample dependency floors against actually published packages. Document the measured provider/engine/platform matrix; test ACAS and WSLC independently before claiming those backends are verified.

Keep online dependency resolution, cloud-backed planning/applying, optional lint/policy tools, variable-dependent initialization and warm reuse as separately bounded follow-ups. None is needed to deliver the validation sibling demonstrated here.
