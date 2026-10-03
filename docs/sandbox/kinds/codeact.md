# CodeAct

`execute_code` runs a Python program in a sandbox. The host chooses the execution environment and enables any file, artifact, network or host-tool access. Without those additions, the tool returns only the execution result.

See the [package README](../../../packages/maf-sandbox-codeact/README.md) for installation and wiring.

## Contract

| Setting | Behavior |
|---|---|
| Kind and tool | `codeact`; `execute_code` |
| Execution | Backend-owned portable Python channel, exec preferred; `CodeactRuntime` remains an explicit alternative |
| Network | Closed unless the host supplies allowed destinations; unrestricted access is unavailable |
| Isolation | The host's minimum; the kind does not raise it |
| Concurrent calls | One call at a time for a conversation's sandbox |
| Cleanup | Disposal by default; no call-directory confinement claim |
| Workload claim | Defaults to `untrusted` in both output modes, carried on `maf_sandbox_derived_integrity`; file evidence can promote the result under the source-channel checks below |
| Result | A [`SandboxResult`](../information-flow.md#the-result-contract): completion, the verdict `ok` or `failed`, the reason a call stopped early, then the program's own text |

Only enabled channels appear in the tool schema. `files` appears with a file store. `outputs` appears with `CodeactOutputs.DECLARED`. The model cannot configure the runtime, sink, registry or network policy.

## Execution and capabilities

| Configuration | Required capabilities | Guest requirements |
|---|---|---|
| Automatic program channel | Channel declares its own execution capabilities | Verified `python-portable-v1`; exec prefers `python3` |
| Python runtime | `RUN_CODE` | Host-verified Python profile |
| Runtime with file input | Add `FILES_IN` | Verified runtime storage base |
| `DECLARED` or `MANIFEST` outputs | Add `FILES_OUT` | Declared or manifest-selected relative paths |
| Automatic host tools | Add `HOST_TOOLS`; channel adds physical capabilities | Shared guest API; exec transport needs `FILES_OUT`, `sh` and `nohup` |

Output collection uses literal paths and never requires `FILES_LIST`. The exec host-tool channel needs `FILES_OUT` even without artifacts because it reads request files and completion markers.

The spec declares no OS family. The selected channel verifies the portable profile before execution; the host still supplies an image with the channel's launcher utilities. Selection and initial availability fallback follow [program-channel admission](../program-channels.md). Explicit `CodeactRuntime` remains host-selected and does not participate in that fallback.

## Files and artifacts

Files come from the caller's listing and are read through the session. Names and transfer limits are checked before guest execution. Each call uses its own directory, except for the explicit flat-storage runtime mode below.

| `CodeactOutputs` | How output names are chosen |
|---|---|
| `NONE` | No artifact collection |
| `DECLARED` | The model supplies `outputs` in the tool call |
| `MANIFEST` | The program writes names into `outputs.json` |

Both collection modes use `outputs_named_at_call_time`. They require an output sink. Returned references identify files that the sink accepted.

| Exec layout | Program | Shared files and outputs |
|---|---|---|
| No host tools | `<call>/program.py` | `<call>/` |
| Host tools enabled | `<call>/host_tools/program.py` | `<call>/work/` |

`program.py` is reserved only for exec without host tools. `outputs.json` is reserved in manifest mode. A runtime writes no program file and may use `program.py` as an ordinary file name.

The automatic path uses `ProgramRequirements.max_program_bytes` independently of shared-file budgets. The program's byte limit is checked before reading the store. Input count is checked before listing, and each input's bytes are checked as it is read. Output collection applies the declared file and byte limits.

## Network access

The kind itself needs no network. The host's `egress_allow` adds permitted destinations:

- Empty list: `CLOSED`.
- Nonempty list: `ALLOWLIST`.
- Method-scoped rules also require `EGRESS_METHODS`.

There is no setting for `UNRESTRICTED`. Each allowed destination can receive anything the program can read, including shared files and host-tool results.

Entries must follow the [network rule syntax](../network.md#method-scoped-allow-entries). A bare string is refused; pass a collection. Invalid hosts are refused and duplicates are removed. The tool description names the allowed destinations.

The router requires a backend that enforces the selected mode. It does not quietly replace an allowlist with closed access.

For [external gateway credentials](../hosts.md#credentials-for-guest-http-requests), pass `credential_retention_seconds=300` and concrete `EgressRule(..., authority="audience")` entries to `make_codeact_tools` or `codeact_sandbox_spec`. These options require each other. The spec requires `ATTACHED_IDENTITY`, per-sandbox sharing, the stated retention ceiling and a fresh sandbox per call. The tool requires approval. The host separately configures the Docker/WSLC credential provider and permits `max_identity_scope=IdentityScope.PER_SANDBOX` on the router. No credential or user selector is exposed as a model tool argument.

## Host tools

No host function is callable until the host supplies a nonempty registry. Reading the registry seals it, so finish registration before building the spec or tools.

Registered functions run in the host process. Guest calls to them bypass the agent's ordinary tool middleware; policy for the enclosing `execute_code` call uses the registry's combined declarations.

![The model's execute_code call passes the host's policy for integrity, outbound confidentiality and required approvals. Inside that call, session reads stage selected store files for the guest. The guest can exchange data with allowed network destinations and, in exec mode, registered host functions. Network enforcement belongs to the backend. Host functions run in the host and bypass ordinary agent tool middleware, while registry controls still apply. Artifact collection checks file and byte limits before OutputSink delivery. These routes act during execute_code, and hiding its untrusted report does not undo them. The run_code variant has no host-tool channel.](../assets/codeact-data-routes.svg)

| Registry declaration | Effect on `execute_code` |
|---|---|
| Any `Identity.USER` tool | Require approval for the whole call |
| A sink, or an undeclared outward-flow role | Apply the host's outbound confidentiality cap |
| Declared identities | Let the router reject denied identities |
| Transport limits | Check the selected channel can serve the complete physical traffic |

Trusted registered sources alone do not make CodeAct output trusted. Core can promote the workload claim when at least one file was read and every accepted read is trusted, or when no files were read and the framework establishes trusted conversation and argument labels. Both paths require closed egress and a registry with only trusted sources or no sources. Configured network access or any untrusted or unknown registered source keeps the claim untrusted. See [host tools](../hosts.md#host-tools-calling-outward) for registration and authority.

The Python source never becomes a shell command. Exec without a registry uses fixed argv. The host-tool launcher uses only fixed or host-generated, quoted paths.

## Result labels and tool flow

The guest program can print data from any enabled source. CodeAct therefore claims `untrusted` for its workload result, including when guest text is withheld.

The kind uses the [result contract](../information-flow.md#the-result-contract) in **both** modes. `verdict` is `ok` or `failed`, from the program's exit status as one bit — eight bits are what a program chooses, and one is what a model can act on without the text. `completed` is false where no exit status was obtained: a refusal, a file that could not be staged, a timeout, or a transport failure. A call without an exit status carries no verdict.

A call that stopped early puts a host-authored explanation in `trusted_output`, so a model can read why the call stopped. Variable diagnostics remain in `output`, whose label follows the file-read, call-evidence and source-channel checks, including file-store names, byte counts, provider errors and partial guest stdout. Withholding mode still omits guest stdout from timeout diagnostics.

Both modes are raised to `trusted` at the framework, not just the withholding one. The completion line and the verdict have to stay readable, and on `agent-framework-core` 1.19 only the tool's own declaration can keep an item there.

![CodeAct declares trusted framework integrity in both modes so completion, verdict and host-authored reasons stay readable. Program text and variable diagnostics follow file-read, call-evidence and source-channel checks. With no reads, trusted framework conversation and argument labels permit promotion under the same source-channel checks; otherwise the untrusted workload claim remains. Withholding mode adds trusted route guidance and omits guest stdout and stderr. Content keeps host-controlled confidentiality. FIDES shows text or hidden references to the model. The next model-called tool is checked against its integrity opt-in and confidentiality limit. Guest host-tool calls and artifact delivery belong to execute_code itself.](../assets/codeact-information-flow.svg)

The host supplies result confidentiality. Hiding applies only while the conversation is trusted, automatic hiding is enabled and the tool is not `inspect_variable`. Hidden output still affects confidentiality. See [information flow](../information-flow.md).

Network access, artifact delivery and guest host-tool calls happen during `execute_code`. They are separate from the later model-called destination shown in the diagram.

## Withholding guest output

`withhold_guest_output=True` removes guest stdout and stderr from the tool result. It requires `CodeactOutputs.DECLARED`. The program must write useful content into declared artifacts.

| Showing mode | Withholding mode |
|---|---|
| Execution report includes guest output | Report includes completion facts without guest streams |
| Uses the sink's display reference | Uses safe declared names, or the sink's per-call folder |
| Collects artifacts on successful execution | Also collects artifacts after a failed program |
| No standing guidance | Adds fixed guidance explaining where to retrieve output |

The wrapper labels the report using the same file-read, call-evidence and source-channel checks, and labels the guidance trusted. Withholding prevents captured guest text from entering the result even when automatic hiding is off. Withholding alone does not make the remaining facts trusted.

Completion and the presence of each declared file still reveal information. `files_out.max_files` bounds the number of file-presence signals per call. Repeated calls can reveal more.

With a `per_call` sink, guidance names the host-generated call folder instead of listing landed files. A host exposing that folder controls access through its own file-reading tools. The sink's `display` value is not used in withheld results because it may contain guest text.

For a fixed landing folder, configure `OutputSink(locate=lambda name: f".tool-results/execute_code/{name}", deliver=deliver)` and have `deliver` write to that same location. `locate` is a pure synchronous function of the validated, sink-normalized declared name and host configuration; it must not read guest content or delivery state. CodeAct calculates the locations before delivery and reports them in the withheld Saved and not-written lists. A receipt whose `name` or `handle` matches confirms the location; a mismatch logs a warning and reports the original declared name. Receipt text is never rendered. Names substituted by the framework still appear only as `outputs[i]` positions.

The default locator is the identity map, preserving existing results. A custom locator removes the description's promise to edit the original file in place: the edited copy lands at the configured location. For `per_call` sinks, locations are relative to the host-minted call folder and the folder-only result stays unchanged. The filesystem sink locates names relative to its root, never by an absolute host path; the file-store sink's destination is `<call_id>/<locate(name)>`.

Names expanded from hidden references are reported by position. `NONE` is refused because it leaves no artifact route. `MANIFEST` is refused because the guest chooses its returned names.

Timeout messages are rebuilt without captured guest output. Host-supplied `output_reason` and stderr explicitly marked `producer_owns_stderr` remain visible in the report. These explain missing or truncated output and are not guest stream text.

## The explicit Python runtime variant

Pass `runtime=CodeactRuntime(...)` to select `run_code`. `RUN_CODE` alone does not identify Python. The profile must describe the environment the host has verified: statement execution, stdout/stderr, no last-expression echo, and available modules.

Without `guest_work_dir`, the runtime has no file channels. File channels require an absolute normalized POSIX storage base other than `/`, writable through Python `open`.

| Runtime storage | Behavior |
|---|---|
| `use_call_directory=True` | Requires `os.makedirs`; creates a child directory for each call |
| `use_call_directory=False` | Uses the supplied base directly; requires at least reset, with disposal as fallback |

The program receives the absolute `guest_call_path`. Its current directory is unchanged. Files and manifests use names relative to that base. The submitted program executes in the runtime's active module namespace, preserving imports and pickling behavior.

Submitted UTF-8 source and its bootstrap spend the inbound byte budget but no file slot. Shared files spend the same total budget. Output collection completes before reset or disposal can remove files.

The execution timeout must be finite and positive. It includes backend queue time. A `SandboxQueuedTimeout` says the program never started and may be retried unchanged; a program timeout is a different result.

The execution choice and profile form an `execution_contract` for instances known to that router. Changing it requires disposal or a new key. The host must keep the profile consistent with the backend.

Nonempty runtime host-tool registries are refused. [Hyperlight](../backends/hyperlight.md) provides a packaged Python profile and an opt-in flat-file profile; use its documented limits.

## Calls, failures and cleanup

`exclusive_admission=True` prevents sibling calls from reading each other's files or exposing withheld content. Queued calls have a bounded wait that accounts for execution and cleanup ahead of them.

Core owns call directories and cleanup. Default disposal means the next call starts in a new sandbox. A fresh directory alone does not establish safe warm reuse.

No router or backend means no attached tool. Unsupported configuration raises during attachment. Expected input and execution failures return a report in the chosen output mode. Cancellation follows core cleanup.

Provider details stay in host logs. A control-plane timeout is not reported as proof that the guest program timed out.

## Status

| Contract | State | Details |
|---|---|---|
| Exec, file channels, output modes and exec host tools | Implemented | [Package README](../../../packages/maf-sandbox-codeact/README.md) |
| Withheld output locations calculated from the sink | Implemented | [#1682](https://github.com/sokolaidev/maf-extensions/issues/1682) (closed) by [#1683](https://github.com/sokolaidev/maf-extensions/pull/1683) (merged) |
| Explicit Python runtime | Implemented | [Hyperlight profiles](../backends/hyperlight.md) |
| Native runtime host tools | Open; nonempty registries are refused | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
| Inherited deployment network defaults | Open; hosts supply explicit allowlists | [#403](https://github.com/sokolaidev/maf-extensions/issues/403) (open) |
| Four-field result contract | Implemented for CodeAct, its samples and live checks | [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (closed) by [#1369](https://github.com/sokolaidev/maf-extensions/pull/1369) (merged) |
