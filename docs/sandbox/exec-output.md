# Exec output: bytes and text

`ExecResult.stdout_bytes` and `stderr_bytes` are the authoritative returned streams. They preserve NUL, CRLF, every non-UTF-8 byte, valid multibyte text, and genuine U+FFFD. Producers construct results with those byte fields before decoding. Encoding an SDK's already-repaired text cannot recover program output; a backend must capture before that boundary or refuse the operation. The same rule applies to future `RUN_CODE` producers; Docker, WSLC and ACAS currently refuse that capability.

`stdout_text` and `stderr_text` are UTF-8 display views using replacement decoding. The existing `stdout` and `stderr` properties are aliases for these safe text views. Codeact and Bicep consume the explicit text views, which can pass through strict UTF-8 JSON transport without lone surrogates. Storing an artifact or counting returned bytes uses the byte fields, never a re-encoding of a text view.

```python
from maf_sandbox import ExecResult

result = ExecResult(stdout_bytes=b"ok\xff\xfe", stderr_bytes=b"", exit_code=7)
assert result.stdout_bytes == b"ok\xff\xfe"
assert len(result.stdout_bytes) == 4
assert result.stdout_text == "ok\ufffd\ufffd"
```

Text-only producers and existing test fixtures can still use `ExecResult("text", "diagnostic", 7, False)`; those inputs are encoded as strict UTF-8. Do not supply text and bytes for the same stream. The dataclass stores byte fields, so `dataclasses.asdict(result)` contains bytes and is not a JSON-ready text payload. Serialize explicit text properties for display or explicitly encode the byte fields, such as with base64, when transporting binary output. `dataclasses.replace` changes `stdout_bytes`/`stderr_bytes` rather than the display properties.

## Ownership, caps and timeouts

`producer_owns_stderr` retains its meaning: false assigns each stream to the corresponding program stream; true reserves stderr for producer diagnostics and moves the program's stderr into stdout alongside its standard output. Byte fidelity applies to what was returned, not to bytes omitted under a documented cap. A producer returning reduced output must make the omission readable without mislabelling its diagnostic as program text. An implementation can instead refuse an oversized capture without returning an `ExecResult`.

The host-tool transport preserves the bytes it reads on completion and exposes timeout partial output through `SandboxProgramTimeout.output_bytes`; `output` and the timeout message remain safe display text. Its existing output cap and timeout diagnostic excerpt still limit how much is returned. Request and control-message decoding remains strict UTF-8: malformed host-tool arguments are refused, never repaired into a different request.

The command timeout covers execution, output retrieval and successful scratch cleanup. Stopping remote work after failure can require a separately documented bounded cleanup allowance. `PosixGuestSubject.exec_cleanup_timeout` lets conformance account for that declared allowance, up to 30 seconds; the default remains zero.

## ACAS capture

The byte contract also applies to `BoundedExec.exec_bounded`. Docker and WSLC bound the combined subprocess pipes before constructing results from raw bytes. ACAS uses guest capture for this surface too: it checks the combined program-output size before retrieval and streams each encoded control response under `max_output_bytes` before parsing it. Encoding and framing can exhaust that response budget before program bytes reach the limit; a budget too small for framing refuses even an empty program result. The configured ACAS per-stream capture limit still applies. Budget overflow raises `SandboxExecOutputLimitExceeded`, and failed bounded capture has the same invalidation and disposal behavior as ordinary exec.

ACAS captures before the service's lossy JSON response. Two FIFO readers retain at most `exec_output_limit_bytes + 1` bytes each while draining inherited writers to EOF, so delayed background output is observed. The extra byte detects overflow. The default limit is 1 MiB per stream; configure `AcasSandboxConfig.exec_output_limit_bytes` to change it. A command can continue writing after that bound, but the capture files do not continue growing. The caller's deadline still bounds the drain.

Retrieval runs `dd` and base64 as the guest in 48 KiB chunks. The adapter checks framing and exact decoded lengths before joining the bytes. This avoids both the measured service ceiling for a single large base64 response and privileged file-plane reads through guest-controlled scratch paths. The SDK still buffers each text response; an oversized or malformed response is refused before decoding it into a stream. A guest can alter its own output or command helpers; capture is not an authenticity boundary against that guest.

EXEC and HOST_TOOLS acquisition requires a compatible `sh`, `mkdir`, `mkfifo`, `head -c`, `cat`, `wc -c`, `dd`, `base64`, `rm` and `rmdir`, plus writable `/tmp`. The compatibility probe creates and removes guest-owned scratch. A missing helper or unwritable scratch refuses acquisition, including for an exec-only caller. The program inherits its original umask. Each call uses a private randomly named directory; all accesses and cleanup execute as the image user.

Timeout, cancellation, overflow, failed readers, malformed framing, interrupted retrieval or failed cleanup invalidates and attempts to delete the entire sandbox, including concurrent commands and its filesystem state. A successful result cannot be returned from an instance invalidated by another call. Deletion has an additional allowance of `min(30, read_timeout_seconds)` seconds. If deletion fails, the original exception carries a note, the backend retains the invalidated entry for disposal, and reacquisition must successfully retry deletion before creating a replacement. These failures do not return partial `ExecResult` streams.

Pending ACAS disposals must complete before acquisition for the affected kind. A key-wide discovery without a known kind blocks every kind for that key. An instance discovered only by a scope purge blocks acquisition across that scope and thread until its deletion succeeds; other scopes and threads remain available. Discovered IDs are retained even when a later listing fails or is cancelled. Acquisition retries those known IDs without requiring another successful listing.

## Sample 09 and release migration

Sample 09 captures subprocess pipes as bytes, then deliberately translates host-root path spellings back to its guest work directory. With the new core it preserves all bytes outside those substitutions. It is a path-translating demonstration, so its result is not a byte-identical copy of a process stream containing one of those host paths. Its published older-core compatibility branch returns UTF-8 display text until the automated samples floor update follows the dependent releases.

This is a breaking release because the result's dataclass representation changes and ACAS adds image prerequisites, an output limit, and disposal on capture failure. The adapting backends and text consumers require the prepared core 0.38 line (`>=0.38.0,<0.39`). Publish core before its dependents; merge the automated samples floor update after the dependents publish. Package versions and changelogs remain owned by release-please.

## Status

| Item | Status | Tracking |
| --- | --- | --- |
| Returned-byte fidelity and safe display views | shipped — core, host-tool transport, Docker, WSLC, bounded ACAS capture and text consumers | [#465](https://github.com/sokolaidev/maf-extensions/issues/465) (closed) by [#1100](https://github.com/sokolaidev/maf-extensions/pull/1100) (merged) |
