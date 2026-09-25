# Execution output

When a tool runs a command or program in the sandbox, the backend returns its captured output and available exit status to the tool in the host application as an `ExecResult`.

`ExecResult` keeps returned stdout and stderr as bytes. Its text properties provide safe display views. Use bytes for storage and byte counts; use text for messages to the model.

![A program produces stdout and stderr bytes. ExecResult stores the captured bytes unchanged in stdout_bytes and stderr_bytes. UTF-8 replacement decoding produces stdout_text and stderr_text for display, with stdout and stderr as aliases. Binary storage uses the byte fields directly. Decoding can lose information, so encoding a display view cannot recover the original bytes. Caps and producer diagnostics remain explicit.](assets/exec-output-bytes.svg)

## Bytes and display text

| Field | Contract |
|---|---|
| `stdout_bytes`, `stderr_bytes` | Authoritative returned streams, including NUL, CRLF and non-UTF-8 bytes |
| `stdout_text`, `stderr_text` | UTF-8 views with replacement characters for invalid sequences |
| `stdout`, `stderr` | Aliases for the text views |
| `exit_code` | Program exit status, when available |
| `producer_owns_stderr` | Whether stderr contains producer diagnostics instead of program stderr |

```python
from maf_sandbox import ExecResult

result = ExecResult(stdout_bytes=b"ok\xff\xfe", stderr_bytes=b"", exit_code=7)
assert result.stdout_bytes == b"ok\xff\xfe"
assert len(result.stdout_bytes) == 4
assert result.stdout_text == "ok\ufffd\ufffd"
```

Construct results from byte fields before decoding. A backend must capture before any lossy SDK conversion or refuse the operation. The same rule applies to `RUN_CODE` results.

Text-only producers can use `ExecResult("text", "diagnostic", 7, False)`. These strings are encoded as strict UTF-8. Do not supply text and bytes for the same stream.

`dataclasses.asdict(result)` contains bytes. For JSON, serialize explicit text views or encode binary data, for example with base64. `dataclasses.replace` changes the byte fields, not the display properties.

## Ownership, caps and timeouts

When `producer_owns_stderr=False`, each field belongs to the matching program stream. When it is `True`, stderr belongs to the producer and program stderr is carried in stdout alongside program stdout. A kind must keep producer notes separate from guest text when labelling its result.

Byte fidelity covers the bytes returned. A documented cap can omit output, but the result must report that omission without presenting a producer note as program text. A backend can also refuse an oversized capture without returning an `ExecResult`.

`BoundedExec.exec_bounded` limits combined stdout/stderr before buffering. Docker and WSLC bound subprocess pipes, and hold plain `exec` to 8 MiB combined: an overflow raises `SandboxExecOutputLimitExceeded` and discards the container, as a timeout does. ACAS bounds program capture and encoded response frames; framing can exhaust its budget even when program output fits.

The host-tool transport exposes timeout partial output as `SandboxProgramTimeout.output_bytes`. Its `output` property and exception message are display text. Existing output and diagnostic-excerpt caps still apply. Host-tool request decoding stays strict UTF-8; malformed requests are refused.

The command deadline includes execution, output retrieval and the scratch-cleanup attempt. A backend may declare a separate bounded allowance to stop failed work. Conformance reads this through `PosixGuestSubject.exec_cleanup_timeout`, defaulting to zero and capped at 30 seconds.

## ACAS capture

ACAS captures program bytes inside the guest before the service converts them to JSON text.

| Step | Behavior |
|---|---|
| Capture | Two FIFO readers drain inherited writers to EOF, retaining at most the per-stream limit plus one overflow-detection byte |
| Limit | `AcasSandboxConfig.exec_output_limit_bytes`, default 1 MiB per stream |
| Retrieve | Guest `dd` and base64 in 48 KiB chunks; check framing and decoded lengths before joining bytes |
| Clean scratch | Remove the private guest directory using guest authority |

Acquisition for `EXEC` or `HOST_TOOLS` checks the required helpers and writable `/tmp`. See [ACAS](backends/acas.md) for the command list and configuration. The guest can alter its helpers or output, so capture does not make the bytes trustworthy.

Timeout, cancellation, overflow, malformed capture or a cleanup exception invalidates the whole sandbox. The backend attempts deletion, and reacquisition must finish any pending deletion before creating a replacement. Concurrent commands can lose that shared instance.

A deadline reached during an observed HTTP 429 `Retry-After` sleep is the narrow exception: no retry started, so the sandbox stays reusable. Other retryable responses and direct cancellation remain uncertain and invalidate it.

After complete retrieval, a reported scratch-removal failure logs a warning and preserves the result. An exception or timeout during that removal still invalidates. Removal never falls back to privileged file access. The [backend guide](backends/acas.md) owns disposal allowances and recovery rules.

<a id="sample-09-and-release-migration"></a>

## Local sample

[Sample 09](../../samples/09_inprocess_bicep/) translates host-root path spellings back to guest paths in captured output. Bytes outside those substitutions are preserved. Output containing a substituted path is intentionally not a byte-identical copy of the original stream.

## Status

| Decision | State | Tracking |
|---|---|---|
| Returned bytes and safe display views | Implemented | [#465](https://github.com/sokolaidev/maf-extensions/issues/465) (closed); [#1100](https://github.com/sokolaidev/maf-extensions/pull/1100) (merged) |
| Complete ACAS results survive reported scratch-removal failure | Implemented; exceptions still invalidate | [#1153](https://github.com/sokolaidev/maf-extensions/issues/1153) (closed); [#1155](https://github.com/sokolaidev/maf-extensions/pull/1155) (merged) |
