# In-process test backend

`InProcessSandboxBackend` and `InProcessSandbox` test kinds and router policy without an engine or service. Commands and programs return scripted results; they are not executed. Files live in an in-memory store.

Import them explicitly from [`maf_sandbox.testing`](../../../packages/maf-sandbox/src/maf_sandbox/testing.py). They are not exported from `maf_sandbox` and provide no production isolation.

![A test configures declarations, scripted execution results and an in-memory file store. The kind and router call the fake through the normal protocols. The fake returns results and records acquisition, commands, programs and cleanup for assertions. Passing these tests checks kind and policy behavior; a real backend's confinement and network claims require separate provider tests.](../assets/in-process-test-flow.svg)

## Defaults

| Setting | Default |
|---|---|
| Isolation | `NONE`; router tests must allow that floor |
| Capabilities | `EXEC`, `FILES_IN`, `RECLAIM` |
| Network declarations | `CLOSED`, `ALLOWLIST`; neither is enforced |
| Transfer limits | `DEFAULT_SANDBOX_LIMITS` |
| Guest OS | None |
| Sharing | One fake sandbox for every key and kind |
| Egress observation and attached identity | None |

Use `dataclasses.replace(FAKE_BACKEND_DECLARATIONS, ...)` to change declarations. A bare `BackendDeclarations` has no egress modes and therefore refuses attachment. Declaring a capability in a test does not add a security boundary.

Set `sandbox_per_key=True` for a separate store per key and kind. Tests of `CALL` scope need this plus the matching declaration, so sibling calls cannot share one store.

## Results and records

The `outputs` mapping matches substrings of command lines or program text. `run_code` can return scripted results, but `RUN_CODE` is not declared by default. Tests requiring it must opt in.

| Record | Contents |
|---|---|
| `keys`, `specs` | Successful acquisition requests |
| `commands` | Command, working directory and timeout; argv is shell-quoted for recording |
| `programs` | Code and timeout |
| `reclaims` | Directory, working directory and timeout |
| `disposed` | Disposal keys |
| `purged` | Scope and thread; `purge_count` controls the reported count |

`acquire_error` raises before recording acquisition. `dispose_failure` and `purge_failure` return structured cleanup failures. `dispose_error` deliberately raises to test a caller's handling of a backend that breaks the best-effort disposal contract.

## File behavior

`seed_files` accepts bytes for regular files or an `EntryKind` for entries without content. Paths are normalized guest paths. A seeded symlink has no target.

The file methods apply the shared confinement checks. `read_file` serves only regular files and refuses an exceeded `max_bytes`; it never truncates. Removal and reclamation change the store, so tests can assert both cleanup calls and their effects.

`storage_base` supports allocation tests with POSIX or Windows-style bases and survives reset. The file-method confinement grammar remains POSIX.

## What a passing test means

The fake checks protocol shape, result handling and router policy. It cannot prove process isolation, safe OS path resolution or network enforcement. Real adapters must also run their conformance tests against a real provider. See [writing a backend](writing-a-backend.md).

## Status

| Area | State | Reference |
|---|---|---|
| Scripted execution and operation records | Implemented | [Core package](../../../packages/maf-sandbox/README.md) |
| File store and separate key/kind stores | Implemented | [Storage contract](../capabilities.md) |
| Security boundary | None; testing only | [Backend isolation](../policy-isolation.md) |
