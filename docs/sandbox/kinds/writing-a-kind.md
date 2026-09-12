# Writing a kind

A kind turns a workload into a tool: choose the inputs, describe the sandbox it needs, run the workload through the protocol, and return its result. Core attaches the tool, checks backend suitability, owns the call's lifetime, and labels the returned items. The host supplies conversation identity, file provenance, and confidentiality policy.

This guide builds a JSON syntax checker with one file input and a split result. It requires the wrapper-owned result contract described in [information flow](../information-flow.md#how-core-labels-a-call), introduced for core 0.37. For a complete application that also collects artifacts, see the [diagram sample](../../../samples/07_docker_diagram/README.md).

## Decide the contract before writing the body

| Decision | JSON checker | Who owns it |
|---|---|---|
| Model input | One name from the caller's file listing | Kind |
| Guest operations | Write one input, execute a fixed Python program | Kind, through `Sandbox` |
| Sandbox requirements | `EXEC`, `FILES_IN`, POSIX paths, closed egress | Kind's `SandboxSpec`; router matches a backend |
| Guest software | An image with `python3` installed | Kind documents it; host selects the image |
| Scope and thread | Read from the current request | Host's `CallerContext` |
| Guest path and cleanup | Use `session.guest_call_path()` and `session.acquire()` | Core |
| Derived result integrity | Explicitly `untrusted`, including a one-word verdict | Kind's source declaration |
| Standing guidance | One unconditional sentence that says how to interpret a hidden result | Kind commits the text; core stamps it |
| Result confidentiality | The host's classification of this tool's results | Host |

Keep backend packages and SDKs out of the kind. A kind imports the core protocol and its own workload dependencies; the application imports the backend and constructs the router. `requires_os_family=OsFamily.POSIX` describes path and command grammar. It does not establish that Python is installed.

## Build one tool

The factory below is complete. Supply a router, a file store, and a caller context from the host. Every normal return passes through `check_json`, so missing context, absent files, timeouts, and successful checks all include the same guidance. The result reports whether Python parsed the file; it makes no claim about schema validation or the meaning of the JSON.

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
    agent_dir: str,
    context: CallerContext,
    *,
    image: str | None = None,
    file_store_provenance: FileStoreProvenance | None = None,
) -> list[Any]:
    return sandboxed_tool(
        lambda session: _build_json_tool(session, file_store),
        router=router,
        context=context,
        agent_dir=agent_dir,
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

`SandboxSpec` supplies `EXEC` and `FILES_IN` by default. The factory returns `[]` when no router or backend is configured. A configured backend that cannot satisfy the spec raises at attach; the host must correct the configuration. Do not catch that refusal and advertise the tool anyway.

The builder is defined at module level because the returned function's docstring becomes the model-facing tool description. The model supplies only `file`; scope, thread, image, and agent directory remain host configuration. Keep those out of the tool signature.

## Keep file reads and guest work inside the call

Resolve the model's file argument against `session.list_files(store)` and pass the resulting `ListedFile` to `session.read_file`. Constructing a replacement entry from the name loses the host's integrity evidence. Calling `store.read` directly bypasses the per-call file fold.

The returned `Content` carries source-integrity metadata for the bytes read. It is not a complete FIDES result label and must not be copied into a `security_label`. Core records successful reads itself, including an empty file; the kind needs no accumulator. A refused or absent read contributes nothing.

The example never echoes a file name, so a name expanded from hidden content cannot leak through its errors. If the tool needs to display names, ask `positions_holding_hidden_content` before calling host code that can change the hidden-content store, then pass the position and verdict to `echoed_name`. A file's integrity label says nothing about whether its name may be shown; see [rewritten arguments](../information-flow.md#the-call-arguments-have-already-been-rewritten).

Use a fixed guest basename and a sequence of command arguments. The model's file name never becomes a guest path or a shell fragment. The byte check bounds transfer into the guest; `AgentFileStore.read` has already loaded the text, so a host needing a bound on that read must enforce it in its store. Production kinds should also log sanitized failure details through `error_detail`, while returning fixed messages to the model.

Set `work_dir=None` unless an image requires a fixed native base. Address the base with `working_directory="."`; `guest_call_path()` is a relative child. Pass filenames relative to the requested working directory, including inside argv. Obtain the sandbox through `session.acquire` and keep owned files beneath `session.guest_call_path`. Core applies cleanup when the body returns or raises. Do not dispose the sandbox yourself or start work that outlives the call. Set `confined_to_guest_call_path=True` only when the kind attempts to confine its changes to that directory, and test the claim with real-backend filesystem and process probes. This metadata does not prove complete cleanup or authorize reuse; the host must explicitly lower its default disposal floor. [Tool-call lifetime](../tool-call.md) owns the full cleanup contract. Set `exclusive_admission=True` when the kind's program can read what a sibling call put in the sandbox, and pass the body's bound as `sandboxed_tool(admission_timeout=...)`: calls then run one at a time, and under the default cleanup each pays a disposal.

## Return derived content first and guidance last

The body returns unlabelled `Content` items. Put every call-dependent answer before the committed guidance: success or failure, diagnostics, counts, sizes, file lists, and conditional advice are all derived. A fixed string such as `"JSON check failed."` remains derived because the file determines which string is returned.

Guidance must be public, true on every return path, and independent of input in both text and presence. Keep private host configuration out of the commitment. Core matches the exact trailing sequence, rebuilds it from the commitment as plain text, and stamps it trusted/public. It rejects missing or reordered guidance, a guidance-only result, a bare string when guidance was committed, and any body-supplied `security_label`. Matching text earlier in the result remains derived.

If no guidance is needed, omit `standing_guidance` and return a string or a nonempty list of unlabelled items. For a route that names the call, a committed sentence may contain `{call_id}`; render the same value in the body's trailing text using the async call's id from `session.guest_call_path().rsplit("/", 1)[-1]`. No other substitution is allowed. Keep guest output, argument values, and file names out of that sentence.

Do not declare the JSON checker trusted: even its Boolean verdict depends on file content. Leaving `source_integrity` unset delegates to the framework's input-label join or host default, which cannot establish an out-of-band file read. Explicit `untrusted` states the kind's actual limit. Trusted file reads never promote it.

## Let the host supply provenance and confidentiality

These are separate inputs. File provenance says what is known about source integrity. A tool's `confidentiality` declaration classifies its derived results. Core uses both declarations only when `source_integrity` and `confidentiality` are valid framework values; the full [decision table](../information-flow.md#how-core-labels-a-call) describes what happens otherwise.

For this factory, the host wires one record into the listing and the session, and adds the observer middleware to the actual agent chain. Its scope and thread getters must read the current request, rather than return shared placeholders:

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
    router, file_store, agent_dir, context,
    image=json_image,
    file_store_provenance=provenance,
)
for tool in tools:
    tool.additional_properties["confidentiality"] = "private"
```

Pass `tools` and `middleware` to the host's agent configuration. This host example classifies results as `private`; choose the classification that fits the application. Configure declarations before calls begin. `default_confidentiality` on middleware and `max_allowed_confidentiality` on a tool do not enable core's per-call result stamp: the former is a fallback, and the latter limits an outbound sink. Neither supplies the tool's explicit result classification.

The default provenance floor is unknown. A host may assert a trusted floor only for initial and otherwise unrecorded files it can establish as trusted, and must observe writes; [host wiring and its limits](../hosts.md#file-store-provenance--what-a-kind-reads-and-what-it-is-worth) explain the race and record-lifetime requirements. The JSON checker remains untrusted with either floor. Guidance stays trusted/public, while its derived result carries the host's classification.

## Verify the kind's contract

Use `InProcessSandboxBackend` to check command arguments, input placement, attach refusals, and every normal result branch. It returns programmed results and does not run Python or establish real isolation. Run the checker against a real backend and the intended image to verify the executable, timeout, egress, and cleanup behavior.

The checker requires POSIX, so its fake must declare that family too. The fake has no OS-family declaration by default. This test router admits the fake's `NONE` isolation explicitly; a production host chooses its own floor:

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

Exercise the public attached tool, so core's wrapper runs. Check successful parsing, a nonzero exit, missing context, a missing or unreadable file, an input over the limit, a timeout, and a backend failure. On every returned result, assert that derived items precede the same guidance. Through `LabelTrackingFunctionMiddleware`, check that a private derived result stays private and that guidance remains visible in a trusted conversation with automatic hiding enabled.

Core owns label-fold tests; a kind's tests should establish its own sources and return shapes. Add concurrent-call and mixed-file cases when the kind reads several files or shares state. A trusted declaration needs a derivation argument for every source, not merely a green example with a trusted file.

If the kind produces artifacts, extend its spec with `DeclaredOutput`, require `FILES_OUT`, and use the host's `OutputSink` for landing. Return references, not embedded artifact bytes. See [artifact rules](README.md#writing-a-kind-that-collects-artifacts) and the [diagram sample](../../../samples/07_docker_diagram/README.md) before adding that channel.

## Migrate an existing kind

Replace `labelled_result_item(text, SourceIntegrity.TRUSTED)` with `Content.from_text(text)`, and commit eligible guidance in `sandboxed_tool(standing_guidance=(...))`. Remove all body-written `security_label` properties, including labels on derived content. Route every normal return through the same suffix construction. Keep the justified source declaration; changing to wrapper-owned labels does not make a workload trusted.

Expose `file_store_provenance` if the host needs reads checked against the current record, and forward it to `sandboxed_tool`. Leave result confidentiality to host wiring. A package adopting this contract needs core 0.37 or later; move both ends of its bounded dependency range, for example `maf-sandbox>=0.37.0,<0.38` for the 0.37 line.

## Status

| Decision | State | Tracking |
|---|---|---|
| Kinds return unlabelled derived items and committed guidance; core owns the stamps and the per-call file fold | shipped; this guide describes the 0.37 contract | [#881](https://github.com/sokolaidev/maf-extensions/issues/881) (closed) by [#1054](https://github.com/sokolaidev/maf-extensions/pull/1054) (merged) |
