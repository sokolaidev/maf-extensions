# Bicep

`bicep_validate` compiles and lints Bicep templates and parameter files from the agent's file store. It returns compiler diagnostics. A module restore failure means validation is incomplete.

See the [package README](../../../packages/maf-sandbox-bicep/README.md) for installation and wiring.

## Contract

| Setting | Value |
|---|---|
| Kind and tool | `bicep`; `bicep_validate` |
| Required capabilities | `EXEC`, `FILES_IN` |
| Guest software | Bicep CLI and the commands required by the supplied image |
| Network modes | `ALLOWLIST` by default; host may select `CLOSED` or `UNRESTRICTED` |
| Work directory | `/maf-sandbox/work`, with `bicepconfig.json` at its root |
| Isolation | The host's minimum; the kind does not raise it |
| Cleanup | Disposal by default; explicit `Cleanup.RECLAIM` can reuse a supported sandbox |
| Result | Untrusted report followed by trusted standing guidance |

The spec does not declare an OS family. The host must select an image that supports the compiler commands. A backend lacking the required capabilities or network mode is refused at attachment.

## Inputs and execution

1. Resolve requested names against the caller's file listing.
2. Read the original listing entries through the session.
3. Write every selected file into a fresh call directory.
4. Build templates with `bicep build` and parameter files with `bicep build-params`. Run lint as applicable.
5. Format the compiler's diagnostics, then let core clean up the call.

![All selected files are staged before compilation. For each file, Bicep builds the template or parameter file, then lints it. The host's network mode applies to every phase: closed access disables restore; other modes permit restore within their limits. The tool formats every phase report. Restore failures, timeouts and execution or report-parsing failures leave validation incomplete. Other diagnostics report compiler errors and warnings. No diagnostics covers only what was checked; hidden or empty output does not establish success. Every normal return includes fixed guidance, and core cleans up.](../assets/bicep-validation-flow.svg)

All files are staged before compilation so local modules and parameter-file references resolve together. The call directory also holds compiled output, the module cache and the temporary profile.

Paths allow `[A-Za-z0-9._/-]` and reject `..` segments. The listing's key is used for reads. Unsafe names do not cause the listing to be echoed; ordinary missing names can receive suggestions.

Shell commands are fixed templates with one validated path substitution. Build diagnostics come from stderr; lint diagnostics come from stdout. Parameter-file builds discard compiled output. The compiler finds configuration by walking up from the source file.

## Network access

The host chooses the mode. The package fixes the restore allowlist:

| Destination | Purpose |
|---|---|
| `mcr.microsoft.com` | Public module manifests |
| `*.data.mcr.microsoft.com` | Module layer data |
| `aka.ms` | Public module-index redirect |
| `live-data.bicep.azure.com` | Module-index data |

Both destinations in each pair are needed. The allowlist does not grant Azure Resource Manager access or supply credentials. Sandbox identity remains host configuration.

`CLOSED` adds `--no-restore` to build, parameter-build and lint commands. Local templates and local modules still work. Unavailable external modules produce diagnostics and a `MODULE RESTORE FAILED` banner.

The banner is returned for BCP190, BCP191 or BCP192. It tells the model that module type checking is incomplete. An empty or hidden diagnostic report is not proof of a successful validation.

## Result labels and tool flow

The compiler and its input files are sources of the diagnostic text. The kind therefore claims `untrusted`, including for counts and returned error reports.

![Bicep validation is a source tool. Its wrapper declares trusted integrity to the framework while retaining an untrusted workload claim. Diagnostics are untrusted content; fixed guidance is trusted content. Both retain the call's effective confidentiality. FIDES shows text or a hidden reference to the model. Later calls to file writers or other tools face the destination's integrity and confidentiality policy.](../assets/bicep-information-flow.svg)

The wrapper exposes `source_integrity="trusted"` and keeps the workload claim in `maf_sandbox_derived_integrity`. It rebuilds the fixed guidance on every normal return, including refusals.

In a trusted conversation with automatic hiding enabled, FIDES hides the report and leaves guidance readable. The guidance says that unreadable diagnostics are not a clean validation. Hidden content still contributes confidentiality.

The host classifies results and controls destination policy. Passing hidden diagnostics to another tool remains subject to that policy. See [information flow](../information-flow.md).

## Errors and cleanup

Store, backend and transport failures produce short messages identifying the file or phase. Detailed provider errors go to host logs. Names expanded from hidden content are shown by argument position.

Diagnostic formatting removes the call directory from locations and message paths. It applies the allowed display names and uses `an unidentified file` for ambiguous matches. This handles quoted paths, file URIs and native Windows paths.

Unmatched external paths and other URLs remain as reported. This path policy does not remove arbitrary compiler prose.

Core owns cleanup. `confined_to_guest_call_path=True` describes the kind's confinement effort; it does not authorize reuse by itself. The host must explicitly choose reclaim, and the backend must support it. See [call cleanup](../tool-call.md).

## Status

| Contract | State | Details |
|---|---|---|
| Validation, restore controls and diagnostic handling | Implemented | [Package README](../../../packages/maf-sandbox-bicep/README.md) |
| Disposal by default; optional reclaim | Implemented | [Call cleanup](../tool-call.md) |
| Four-field result contract | Open; this kind returns report and guidance items | [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (open) |
