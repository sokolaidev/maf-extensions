> Exploration for [#465](https://github.com/sokolaidev/maf-extensions/issues/465): whether ACAS can return exact exec bytes before choosing a public result representation. These are feasibility measurements, not the shipped backend contract.

# Lossless ACAS exec output

On 2026-09-10, the public `python-3.13` image was exercised through `azure-containerapps-sandbox` 0.1.0b4 and the backend at `7de3c5e1b9aeb82ab3b870b25b61c3c1d1861c16`. The [probe](../../../scripts/probe_acas_exec_bytes.py) creates a sandbox in an existing group, measures three capture strategies, and deletes its own scope. The [final evidence](acas-exec-bytes-evidence.json) contains measurements without deployment identifiers.

The result is encouraging but not a transparent decoder fix: separate pipes drained into files, followed by binary file reads, preserved the tested bytes and background output. Using that path for every ACAS exec introduces a scratch-filesystem and utility contract, output limits, and a lifecycle that have not yet been implemented in the backend.

## The loss precedes SDK decoding

The probe calls the same `executeShellCommand` endpoint as `SandboxClient.exec` and inspects the HTTP response before constructing the SDK's typed result. A program writes `b"ok\xff\xfe"` to stdout and `b"err\xff\xfe"` to stderr, then exits 7. The JSON fields contain `ok` plus one U+FFFD and `err` plus one U+FFFD; the response body contains the UTF-8 bytes for that replacement character. The adapter cannot reverse this loss.

The installed SDK exposes no raw output selector on exec. The installed CLI's `sandbox exec --help` exposes no encoding selector either. Its separate `sandbox shell` is an interactive PTY, which is not evidence for lossless, separate noninteractive streams. The public upstream repository inspected for this work carried documentation and CLI installers rather than the SDK/service implementation; this investigation does not establish that no unpublished API could provide raw bytes.

## What the capture strategies measured

| Strategy | Measurement | Result |
| --- | --- | --- |
| Temporary files, base64 envelope returned through exec | 277-byte corpus including all byte values, genuine U+FFFD, CRLF, and incomplete UTF-8; argv and shell forms | Both streams exact; exit 7 retained |
| Same envelope | Empty streams, quoted argv, three concurrent commands | Exact |
| Same envelope | 65,536 bytes per stream | Exact |
| Same envelope | 1,048,576 bytes per stream | Incomplete response, service exit 137; refused by decoder |
| Direct file redirection and binary file retrieval | 1,048,576 bytes per stream | Both streams exact; exit 7 retained |
| Separate FIFO readers writing files, then binary retrieval | Two concurrent commands, each writing 1,048,576 bytes per stream | All four streams exact; each exit 7 retained |
| Separate FIFO readers writing files | Delayed background writer | Matches ordinary exec's `beforeafter` |

The large envelope received exactly 1,048,576 text characters and this service diagnostic: `[adc] output exceeded 1048576 bytes and was truncated; process terminated`. Its initial frame already carried the program's exit 7, but the service returned 137 after terminating output transmission. Base64 therefore both consumes the available response budget and can change the reported command status. The probe rejects the incomplete frame; it does not call the truncated data a successful byte round trip. This is a measured limit of this path and deployment, not a promise about every service configuration.

There is a second reason to reject simple redirection as the final design. For `printf before; (sleep 2; printf after) &`, ordinary ACAS exec returned `beforeafter`; the temporary-file/base64 wrapper returned only `before`. The inner shell exited before its descendant finished writing, so reading the file then lost output that the ordinary pipe reader awaited. `file_capture_command` instead creates two FIFOs, starts a reader for each, runs the program with those pipes, and waits for both readers before returning the program's status. The live background-writer probe recovered all eleven bytes. Plain file redirection alone does not have that EOF wait.

## Requirements that remain before backend integration

The file strategy needs `sh`, `mkdir`, `mkfifo`, `cat`, `rm`, and `rmdir`, a writable guest directory, and working binary file APIs. The corpus generator additionally uses Python, but the capture wrapper itself does not. Each concurrent capture needs its own directory. These are new requirements for an exec-only caller; the existing ACAS backend can currently serve commands that need no writable guest directory. Refusing or provisioning missing prerequisites needs a deliberate acquisition contract.

The file strategy also bypasses the exec endpoint's observed output ceiling. A production implementation must bound guest spool growth and host reads, specify what happens at the bound, and keep host-generated truncation diagnostics distinguishable from guest stderr. A `read_file(max_bytes=...)` ceiling alone does not bound how much the guest writes before retrieval.

The timeout and cancellation measurements were made against the base64 wrapper: after the client raised `TimeoutError` or `CancelledError`, the remote program still completed its delayed write. A host-side cancelled HTTP request is not a guest-process signal. Ordinary completion eventually removed the base64 scratch directory in those tests, but that does not establish cleanup for an infinite program. The FIFO candidate has not been tested under cancellation, pump failure, interrupted retrieval, hostile file replacement, or a non-root/unwritable image. Its plain success-path cleanup is not a reusable teardown policy.

The next implementation work should therefore define bounded pipe/file capture with a deadline covering launch, drain, and retrieval; cancellation/disposal behavior that does not leave readers or scratch state behind; and acquire-time compatibility checks. The public representation can then store bytes with explicit text views. Keeping surrogateescape strings remains possible, but does not remove any ACAS requirement and still needs display conversion before strict UTF-8 serialization.

The existing ownership flag stays intact, and host-tool request/control-message decoding stays strict. Nothing in this result licenses decoding malformed host-tool arguments leniently.

## Reproducing and interpreting the result

Set the existing live-test variables `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, and `ACAS_SANDBOX_GROUP`, then run from the repository's synchronized workspace:

```powershell
uv run python scripts/probe_acas_exec_bytes.py --live --output acas-exec-bytes.json
```

`--live` is mandatory because the probe creates one billable sandbox. It uses only its generated scope, attempts disposal even when acquisition or a measurement fails, and checks the service's label listing afterward. It does not change the existing group. The three investigation runs each confirmed their scope empty; the final run also confirmed no base64 capture directories remained and successful cleanup of the binary capture files.

The command exits nonzero when a candidate fails byte fidelity or cleanup. On the measured service, the base64 limit case intentionally exposes a failure, so exit 1 is the recorded research finding rather than a green production acceptance result. The thirteen offline tests check that incomplete, malformed, and status-inconsistent envelopes cannot be reported as exact output. The current package implementation and its published result contract are unchanged.
