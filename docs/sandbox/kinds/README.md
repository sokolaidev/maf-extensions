# Kinds

A **kind** is a workload exposed as one or more tools. It defines the inputs, required sandbox features, commands and results. The host selects the backend, identity and policy.

These pages describe each kind's contract. Package READMEs cover installation and wiring. Start with [writing a kind](writing-a-kind.md) to build your own.

## Choose a kind

| Kind | Tools | Purpose | Network access |
|---|---|---|---|
| [Bicep](bicep.md) | `bicep_validate` | Compile and lint templates and parameter files | Fixed restore allowlist by default; host can choose closed or unrestricted |
| [CodeAct](codeact.md) | `execute_code` | Run Python, with optional files, artifacts and host tools | Closed by default; host can add allowed destinations |
| [draw.io](drawio.md) | `create_drawio` | Validate and lay out editable diagrams | Closed |
| [Terraform / OpenTofu](terraform.md) | Validation and optional formatting tools | Check configuration offline; optionally return formatted files | Closed |

## Responsibilities

| Owner | Supplies |
|---|---|
| Kind | Tool schema, sandbox requirements, workload logic and justified result-integrity claim |
| Host | Backend, image, caller identity, file source records, output destination and confidentiality policy |
| Core | Backend selection, call directories, transfer limits, cleanup and result labels |
| Framework | Label propagation, result hiding and tool-call policy |

Kinds and backends use the core protocol. They do not import each other. Repository tests check this boundary and each package's declared dependencies.

A kind runs only on a backend that meets its requirements. An unconfigured router attaches no tool. A configured backend that cannot serve the spec causes an attachment error.

## Tools, content and labels

A kind is a source tool when it returns a result. It can also send data through network access, a registered host tool or an artifact destination. The host must account for each enabled route.

![Source tools declare result integrity and confidentiality. Returned content items have individual effective labels. The framework shows text or a hidden reference to the model and tracks the conversation label. The model's next call is checked against the destination tool's integrity opt-in and confidentiality limit. Hidden items still contribute confidentiality, and an integrity opt-in does not bypass that limit.](../assets/information-flow.svg)

All four kinds claim `untrusted` for workload output. Compiler diagnostics, guest programs, provider reports and layout output can carry content the host has not established as trusted.

Bicep, Terraform and CodeAct's withholding mode also return fixed guidance. The wrapper keeps that guidance trusted and labels workload output separately. draw.io and CodeAct's showing mode have no guidance item.

The [information-flow guide](../information-flow.md) explains label resolution, hiding and the four-field result contract. Each kind page shows its own result flow.

## Define the sandbox requirements

| `SandboxSpec` field | What it controls |
|---|---|
| `kind` | Workload identity; one kind may expose several tools |
| `requires` | Backend operations the workload uses |
| `requires_os_family` | Guest path and command conventions; this does not prove a program is installed |
| `egress`, `egress_allow` | One network mode and its allowed destinations |
| `min_isolation`, `min_cleanup`, `isolation_scope` | Required isolation, cleanup and sharing boundaries |
| `files_in`, `files_out`, `declared_outputs` | Transfer limits and expected artifacts |

Ask only for capabilities the workload needs. Declared outputs require `FILES_OUT`. The router must serve the exact network mode; it does not substitute a more open or closed one.

See [capabilities](../capabilities.md), [network access](../network.md) and [isolation policy](../policy-isolation.md) for the common rules.

## Writing a kind that collects artifacts

1. Declare each output's relative path, media type and whether it is required.
2. Tell the model where the program must write it.
3. Use `collect_outputs` and the host's `OutputSink`. Return delivery references rather than artifact bytes.
4. For names chosen per call, set `outputs_named_at_call_time` and pass the names to `collect_outputs(outputs=...)`.
5. Require `FILES_LIST` only when the workload must enumerate files. Known paths do not need it.
6. Let core derive outward-flow declarations from the sink and spec. Do not combine an output sink with an explicit `declarations=` mapping.

The [diagram sample](../../../samples/07_docker_diagram/README.md) shows a complete artifact-producing tool.

## Writing a kind that declares its information flow

- Declare `untrusted` when any program or input affecting the result is untrusted or unknown. Formatting the text does not change its source.
- Justify every source before claiming trusted output. Include file reads, network responses and host-tool results.
- Resolve file names against `session.list_files` and pass the original `ListedFile` to `session.read_file`. Direct store reads bypass call-level tracking.
- Let the wrapper write result labels. Bodies return unlabelled content, or opt into `SandboxResult`.
- Keep standing guidance fixed and present on every normal return. Use a declared verdict for a trusted choice that varies by result.
- Treat file integrity, hidden names and result confidentiality separately. A file's integrity does not permit echoing its name.

A `nothing_survives_from` assertion needs the author's justification. It does not bypass file-read checks. A trusted host-tool registry establishes that source only; it says nothing about files or network responses.

The [authoring guide](writing-a-kind.md) contains a working example, host setup and checks.

## Status

| Contract | State | Details |
|---|---|---|
| Four kinds, including optional Terraform/OpenTofu formatting | Implemented | [Bicep](bicep.md), [CodeAct](codeact.md), [draw.io](drawio.md), [Terraform](terraform.md) |
| Wrapper-owned labels and file-read checks | Implemented | [Information flow](../information-flow.md) |
| Four-field result contract | Available in core; implemented for Terraform/OpenTofu tools and their live tests; other kinds, samples and their live checks remain open | [Information flow status](../information-flow.md#status) |
| CodeAct native runtime host tools and inherited network defaults | Open | [CodeAct status](codeact.md#status) |
