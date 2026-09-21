# maf-sandbox-codeact

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxCodeactExperimentalWarning`.

Give an agent one `execute_code` tool. The model writes Python statements, the sandbox runs them, and the tool returns what the program printed. Programs must print results; a final expression is not echoed.

This is an independent package for [Microsoft Agent Framework](https://aka.ms/AgentFramework). It uses the `maf-sandbox` protocol and has no backend dependency.

## Quickstart

```bash
pip install maf-sandbox-codeact
```

```python
from maf_sandbox_codeact import make_codeact_tools

tools = make_codeact_tools(
    router,
    "data-analyst",
    context,
    image="mcr.microsoft.com/devcontainers/python:3.13-bookworm",
)
```

The host supplies the router and `CallerContext`. With no configured backend the factory returns `[]`. Unsupported requirements are refused before attachment.

The default path writes `program.py` and runs it with `python3`. It requires `EXEC` and `FILES_IN`. Source text is file content, never part of the command line.

Use the [Docker sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/06_docker_codeact), [ACAS sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/03_acas_codeact) or [WSLC sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/04_wslc_codeact) for complete applications.

## Optional channels

All four channels below are off by default. The host enables them when building the tool.

| Setting | What it enables |
|---|---|
| `file_store=store` | A `files` argument selecting caller-visible input files. |
| `output_sink=sink`, `outputs=...` | Collection and delivery of named output files. |
| `host_tools=registry` | Calls from guest Python to registered host functions, in exec mode. |
| `egress_allow=(...)` | Network requests to named destinations. |

These channels operate during `execute_code`. Hiding its final report does not undo host calls, network requests or artifact delivery.

![The model calls execute_code through host policy. The tool can stage selected store files, run Python, serve registered host functions and collect artifacts. Guest network access follows backend policy. Nested host functions run in the host and bypass ordinary agent middleware. Artifact delivery goes directly to the host's sink. The model receives the execution report after these actions.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/c6479aaa19d14ffcf25f76ea7ff11ce152a16fb2/docs/sandbox/assets/codeact-data-routes.svg)

## Input and output files

Inputs must appear in `CallerContext.list_files`. They are staged under their listed names, so a program can open `data/sales.csv`. The listing controls which files are shared; it does not make their contents trustworthy.

Choose one output mode:

| Mode | Names come from |
|---|---|
| `NONE` | No output collection. This is the default. |
| `DECLARED` | The model's `outputs` argument, checked before execution. |
| `MANIFEST` | The program's `outputs.json`, read after execution. |

Both collection modes require a sink and `FILES_OUT`. They collect literal paths and do not require `FILES_LIST`. The manifest consumes one file slot and part of the byte budget, so that mode needs at least two output slots.

```python
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools

tools = make_codeact_tools(
    router,
    "data-analyst",
    context,
    file_store=store,
    output_sink=sink,
    outputs=CodeactOutputs.DECLARED,
    image="mcr.microsoft.com/devcontainers/python:3.13-bookworm",
)
```

`files_in` and `files_out` bound file count, individual bytes and total bytes. A missing declared output is reported. Artifacts carry no guest-selected media type; the host decides how to handle them.

Keep the output sink separate from the agent's writable input store. Otherwise guest code could overwrite files through a channel that bypasses the host's file-write approval. See the [files sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/08_docker_codeact_files).

## Host functions and network access

Register host functions before passing the registry to the factory. Reading its combined policy seals it against later registration.

```python
from maf_sandbox import HostToolRegistry, TransferLimits

registry = HostToolRegistry(
    max_host_tool_calls_per_run=32,
    response_limits=TransferLimits(
        max_bytes_per_file=64 * 1024,
        max_total_bytes=1024 * 1024,
        max_files=32,
    ),
)
registry.register(exchange_rate)
```

`exchange_rate` is a host-defined function. Its body runs with host authority and bypasses ordinary agent middleware. Configure declarations, permitted identities and approval policy before exposing it. Registering a user-authority function makes the enclosing tool approval-gated.

A nonempty registry requires `HOST_TOOLS` and `FILES_OUT` as well as `EXEC` and `FILES_IN`. Docker and ACAS support this transport; WSLC does not. The image needs the POSIX launcher utilities, including `sh` and `nohup`.

Transport traffic counts toward backend transfer limits. Set response limits to fit your functions; broad defaults can make the tool fail attachment. Model-named files live separately from transport files. Without a registry, `program.py` is reserved.

Network access is closed unless `egress_allow` names hosts. An allowed host can receive any data the program can read. A method-scoped `EgressRule` also requires `EGRESS_METHODS`, which no shipped backend declares.

See [host-tool controls](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#calling-host-tools) for registration, identities and limits.

## Runtime backends

Use `CodeactRuntime` to select `run_code` instead of command execution. The host must verify that the backend evaluates Python statements and returns their output. The required instructions describe the modules and facilities available to the model.

```python
from maf_sandbox_codeact import CodeactRuntime, codeact_sandbox_spec, make_codeact_tools

runtime = CodeactRuntime(
    instructions="Python statements with json and math. No subprocess or network modules."
)
spec = codeact_sandbox_spec(runtime=runtime)
tools = make_codeact_tools(router, "data-analyst", context, runtime=runtime)
```

The stdout-only profile requires `RUN_CODE`. It submits source directly and uses no inbound file slot, but source bytes still count toward the input byte limits. There is no automatic fallback between variants.

File channels require a verified absolute POSIX `guest_work_dir`. Programs receive `guest_call_path` and use it with `open`; model-facing names remain relative. By default the runtime must provide `os.makedirs` for fresh call directories.

Use `use_call_directory=False` only for a prepared base that cannot create directories. That mode requires at least reset cleanup. The [Hyperlight package](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-hyperlight/README.md) supplies matching instructions for its `/output` base.

Runtime profiles do not support host-tool registries. Changing the execution variant, instructions or storage contract requires disposal or a new sandbox key.

## Results and labels

Program output is untrusted. The ordinary report includes stdout, available stderr and a nonzero exit code. With the host-tool transport, program stderr is merged into stdout; a separate `note:` comes from the transport.

FIDES may hide the report while the conversation is trusted. Hidden content still affects confidentiality. The host controls whether later tools may accept it. See [information flow](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md).

`withhold_guest_output=True` removes guest-authored text from the report. It requires `CodeactOutputs.DECLARED` and a sink. The result has an untrusted status item and separate trusted route guidance, so the model can learn how to read saved output through host file tools.

This mode omits sink display text and guest-selected filenames. Exit success and whether declared files landed still depend on the program. Withholding therefore does not make the report trusted or eliminate every data channel.

## Calls and cleanup

Calls run one at a time in each sandbox, including collection and cleanup. `exec_timeout_seconds` defaults to 120. Runtime deadlines include the backend queue; a queued timeout means the program did not start.

Core disposes after each call by default. A host can permit snapshot reset, or accept possible leftover state by explicitly choosing reclaim on a supporting backend.

CodeAct makes no call-directory confinement claim. Programs can access other guest paths and leave processes running. Choose the isolation floor and image for everything the program may read, including shared files and network responses.

The [CodeAct guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/codeact.md) covers execution, withholding and cleanup in detail.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
