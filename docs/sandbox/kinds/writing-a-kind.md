# Writing a kind

A kind exposes a workload as one or more tools. It declares sandbox requirements, runs the workload through the core protocol and returns unlabelled content. Core owns attachment, call lifetime and result labels. The host supplies identity, file provenance and confidentiality policy.

This guide builds a JSON syntax checker. It uses a report followed by fixed guidance. The [result contract](../information-flow.md#the-result-contract) also supports separate completion, verdict and output items. The [diagram sample](../../../samples/07_docker_diagram/README.md) shows artifact collection.

## Define the contract

| Part | JSON checker |
|---|---|
| Model input | One name from the caller's file listing |
| Guest work | Write one file; run a fixed Python program |
| Capabilities | `EXEC`, `FILES_IN` |
| Environment | POSIX paths, closed network, image with `python3` |
| Host inputs | Router, store, caller context, image and result classification |
| Result | Untrusted parse report, then fixed trusted guidance |
| Cleanup | Core's default disposal |

Import the core protocol and workload dependencies in the kind. Keep backend imports in the application. An OS-family requirement describes paths and commands; it does not prove that Python is installed.

## Build one tool

The factory below is complete. Every normal return includes the same guidance. The check reports JSON syntax only; it does not validate a schema or the meaning of the data.

```python
from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import AgentFileStore, Content
from maf_sandbox import (
    CallerContext,
    Egress,
    FileStoreProvenance,
    OsFamily,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
)
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool

GUIDANCE = "A hidden JSON check result is not evidence that the file is valid."
_CHECK = "import json; json.load(open('input.json', encoding='utf-8'))"


def make_json_tools(
    router: SandboxRouter | None,
    file_store: AgentFileStore,
    agent_id: str,
    context: CallerContext,
    *,
    image: str | None = None,
    file_store_provenance: FileStoreProvenance | None = None,
) -> list[Any]:
    return sandboxed_tool(
        lambda session: _build_json_tool(session, file_store),
        router=router,
        context=context,
        agent_id=agent_id,
        spec=SandboxSpec(
            kind="json-check",
            image=image,
            work_dir=None,
            egress=Egress.CLOSED,
            requires_os_family=OsFamily.POSIX,
        ),
        name="check_json",
        source_integrity=SourceIntegrity.UNTRUSTED,
        standing_guidance=(GUIDANCE,),
        file_store_provenance=file_store_provenance,
    )


def _build_json_tool(
    session: SandboxToolSession, store: AgentFileStore
) -> Callable[..., Awaitable[list[Content]]]:
    async def report(file: str) -> str:
        key = session.key()
        if isinstance(key, str):
            return key

        listing = await session.list_files(store)
        if isinstance(listing, str):
            return "Error: could not list inputs."
        listed = next((entry for entry in listing if entry.name == file), None)
        if listed is None:
            return "Error: the requested file is not in the listing."

        content = await session.read_file(store, listed, named="the requested file")
        if isinstance(content, str):
            return content
        if content is None or content.text is None:
            return "Error: the requested file has no readable text."
        limit = min(
            session.spec.files_in.max_bytes_per_file,
            session.spec.files_in.max_total_bytes,
        )
        if len(content.text.encode("utf-8")) > limit:
            return "Error: the requested file exceeds the input limit."

        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox
        guest_path = session.guest_call_path()
        try:
            await sandbox.write_file(
                "input.json", content.text, working_directory=guest_path
            )
            result = await sandbox.exec(
                ["python3", "-I", "-c", _CHECK],
                working_directory=guest_path,
                timeout=30,
            )
        except TimeoutError:
            return "Error: the JSON check timed out."
        except Exception:
            return "Error: the JSON check could not run."
        return "JSON parsed successfully." if result.exit_code == 0 else "JSON check failed."

    async def check_json(file: str) -> list[Content]:
        """Check the syntax of one JSON file from the caller's file store."""
        return [Content.from_text(await report(file)), Content.from_text(GUIDANCE)]

    return check_json
```

`SandboxSpec` supplies `EXEC` and `FILES_IN` by default. No router or backend means an empty tool list. An incompatible configured backend raises during attachment; fix the host configuration before exposing the tool.

The function's docstring becomes the model's tool description. Only `file` appears in its schema. Identity, image and other host choices stay outside the tool signature.

## Read files through the session

Resolve names with `session.list_files`, then pass the original `ListedFile` to `session.read_file`. Rebuilding an entry loses the listing's integrity evidence. Reading the store directly bypasses call-level tracking.

Core records successful reads, including empty files. Missing or refused reads contribute nothing. The kind needs no separate accumulator and must not copy a read's metadata into a result label.

The example never echoes the input name. If a kind displays names, call `positions_holding_hidden_content` before host code can change the hidden-content store. Use `echoed_name` to show a safe name or argument position. See [rewritten arguments](../information-flow.md#the-call-arguments-have-already-been-rewritten).

Use fixed guest basenames and argument lists. Keep model values out of shell commands. The example's byte check bounds transfer into the guest; the store must enforce any limit on loading the file into host memory.

## Keep work inside the call

Use `session.acquire` and `session.guest_call_path()`. Set `work_dir=None` unless the image needs a fixed base. `working_directory="."` addresses that base; the call path is a relative child. Pass file names relative to the selected working directory.

Core cleans up after the body returns or raises. Do not dispose the sandbox in the body or start work that outlives the call. Return fixed error messages and log sanitized details with `error_detail`.

Set `confined_to_guest_call_path=True` only when the kind confines its changes to that directory. Test that claim against real filesystem and process behavior. It does not prove cleanup or authorize reuse; the host must explicitly lower the disposal floor.

Use `exclusive_admission=True` when a program can read another call's files. Give `sandboxed_tool` a bounded `admission_timeout`. See [call lifetime](../tool-call.md) for admission and cleanup rules.

## Return content with separate purposes

Bodies do not write `security_label`. The wrapper owns those labels.

For a text-item result, put all call-dependent content before the exact committed guidance. Reports, counts, file lists and conditional advice are workload output. Even a fixed sentence selected by the input belongs there.

Guidance must be public and true on every normal return. Both its text and its presence must be independent of input. Core checks the trailing sequence, rebuilds it and stamps it `trusted/public`. The framework preserves any stricter confidentiality from the call.

Core refuses missing or reordered guidance, guidance-only results and body-written labels. Matching text earlier in the result remains workload output. Only the host-generated `{call_id}` may vary in a commitment; see [standing guidance](../information-flow.md#one-result-two-labels).

Without guidance, return a string or a nonempty list of unlabelled items. With the four-field contract, return `SandboxResult`; core appends committed guidance itself.

| `SandboxResult` field | Purpose |
|---|---|
| `completed` | Say whether the workload reached an answer |
| `verdict` | Select an answer from values fixed at attachment |
| `trusted_output` | Return text whose sources the kind can vouch for |
| `output` | Return workload text, such as diagnostics |

Opt in with `result_contract=True` and declare `verdicts=(...)`. Keep the workload claim `untrusted` for guest text. The wrapper lets the first three fields inherit trusted integrity while labelling workload output separately.

A trusted verdict selects the kind's own constant; it does not repeat guest text. Do not mark an entire parser or compiler report trusted to make a verdict readable. The [result-contract diagram](../information-flow.md#the-result-contract) shows the separation.

## Let the host supply provenance and confidentiality

File provenance records source integrity. Result confidentiality classifies who may receive the answer. They are separate settings.

Use one provenance record for listing, session reads and write observation. Add the observer to the actual agent middleware chain. Caller-context getters must read the current request.

```python
from functools import partial

from agent_framework.security import LabelTrackingFunctionMiddleware
from maf_sandbox import CallerContext, FileStoreProvenance
from maf_sandbox.maf import file_store_provenance_middleware, list_all_files

provenance = FileStoreProvenance()
middleware = [
    LabelTrackingFunctionMiddleware(),
    file_store_provenance_middleware(provenance),
]
context = CallerContext(
    current_scope=current_scope,
    current_thread_id=current_thread_id,
    list_files=partial(list_all_files, provenance=provenance),
)
tools = make_json_tools(
    router, file_store, agent_id, context,
    image=json_image,
    file_store_provenance=provenance,
)
for tool in tools:
    tool.additional_properties["confidentiality"] = "private"
```

Pass these tools and middleware to the agent. The example classifies results as `private`; choose the application's own value before calls begin. Configure destination policy separately.

With guidance or the result contract, core always labels workload items. Without them, it does so only when both integrity and result confidentiality are valid. A middleware default or `max_allowed_confidentiality` does not supply that explicit result classification.

The default provenance floor is unknown. Assert a trusted floor only for files the host can establish as trusted, and observe writes. [Host wiring](../hosts.md#file-store-provenance--what-a-kind-reads-and-what-it-is-worth) describes record lifetime and concurrent-write limits.

Trusted files never promote this checker's untrusted workload claim. The report remains untrusted. Guidance stays trusted, with the call's effective confidentiality.

## Verify the contract

`InProcessSandboxBackend` checks attachment, arguments, input placement and result branches with programmed responses. It does not run Python or establish isolation.

The checker requires POSIX, so the fake must declare it. The test router also admits the fake's `NONE` isolation explicitly:

```python
from dataclasses import replace

from maf_sandbox import Isolation, OsFamily, SandboxRouter
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

backend = InProcessSandboxBackend(
    declarations=replace(
        FAKE_BACKEND_DECLARATIONS, os_families=frozenset({OsFamily.POSIX})
    )
)
router = SandboxRouter([backend], min_isolation=Isolation.NONE)
```

Exercise the attached tool so core's wrapper runs. Cover successful parsing, a nonzero exit, missing context, unreadable files, input limits, timeout and backend failure. Check that each normal result keeps the report and guidance separate.

Through label-tracking middleware, check that private results stay private and guidance remains readable under automatic hiding. Add concurrent-call cases when calls share state. Verify the executable, network, timeout and cleanup against a real backend and the intended image.

For artifacts, declare outputs, require `FILES_OUT` and collect through the host's `OutputSink`. Return delivery references. Follow the [artifact rules](README.md#writing-a-kind-that-collects-artifacts).

## Status

| Contract | State | Details |
|---|---|---|
| Session file reads, wrapper labels and standing guidance | Implemented; used by this example | [Information flow](../information-flow.md) |
| Four-field `SandboxResult` | Available through explicit opt-in | [Result contract](../information-flow.md#the-result-contract) |
