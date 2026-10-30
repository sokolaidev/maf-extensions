# maf-sandbox-antivirus

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-antivirus)](https://pypi.org/project/maf-sandbox-antivirus/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-antivirus)](https://pypi.org/project/maf-sandbox-antivirus/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-antivirus/LICENSE)

> **Experimental.** Import emits `MafSandboxAntivirusExperimentalWarning`. Not yet published. The first package release requires a completed, eligible `antivirus` image release.

Scan one authorized binary upload in a disposable POSIX sandbox. ClamAV starts from the image's baked seed and applies host-supplied, authenticated updates before the upload is staged. Guest egress remains closed throughout; there is no runtime download or shared scan process.

```python
from pathlib import Path
from maf_sandbox_antivirus import directory_update_provider, make_antivirus_tools

[scan_file] = make_antivirus_tools(
    router,
    quarantine_store,
    "upload-scanner",
    context,
    image=verified_antivirus_image,
    update_provider=directory_update_provider(Path("database-snapshot")),
)
answer = await scan_file.func(file="uploaded-file")
```

The store's `async read(name)` returns `bytes | None`; `context.list_files(store)` supplies authorized `ListedFile` entries. Names match exactly and never become host paths, guest paths or URLs. Hosts must bound ingress before the store allocates bytes. The kind independently refuses inputs over `max_bytes` before acquiring a guest.

The host keeps uploads quarantined until admission succeeds and promotes the exact scanned bytes. A filename is not a content identity: never rescan one value and later publish a mutable store name. The result includes a host-computed input SHA-256. Malware scanning does not promote the upload's source integrity and does not replace document parser guards.

## Database updates

The optional `update_provider` is an async callable receiving three `DatabaseIdentity` objects identifying the exact image seed. It returns an immutable `DatabaseUpdate` containing that seed, target versions, and `UpdateFile` bytes. Each database uses either a consecutive chain of official signed `.cdiff` files or a full signed `.cvd` replacement. Unsigned scripts, CLD replacements, version rollback, gaps and mixed replacement/patch plans are refused. ClamAV verifies CVDs and signed CDIFFs inside the guest; hashes bind transfers but do not replace upstream signature authentication. No provider means use the seed only if its daily database meets the age ceiling.

`directory_update_provider` reads an immutable host-owned mirror snapshot with `main.cvd`, `daily.cvd`, `bytecode.cvd` and optional CDIFFs. It chooses patches when the entire chain exists, otherwise a full replacement. The host owns acquisition, refresh cadence, concurrency and atomic snapshot publication; the kind performs no network access. [ClamAV's cvdupdate mirror guidance](https://docs.clamav.net/appendix/CvdPrivateMirror.html) explains how to retain patches. A plain FreshClam directory normally retains updated databases rather than a distributable CDIFF history. Keep snapshots alive until their calls finish.

Updates are limited to 95 MiB and 90 patches per call. Payloads cross the backend in chunks of at most 16 MiB and are reassembled and hashed in the guest. These defaults fit ACAS's 32 MiB per-file and 128 MiB total ceilings together with the default upload size. An oversized replacement fails closed; use a newer seed image. Every call pays the local patch application and engine-loading cost even when the host reuses cached updates.

## Results and limits

`scan_file.func(file=...)` returns core's labelled content sequence, the same result as an attached tool. The first item is core's completion sentence; a completed result has exactly one fixed verdict item (`Result: clean`, `Result: infected`, or `Result: refused`). Only those wrapper-owned items establish admission. Signature names, guest diagnostics and exception details are excluded from tool results. Hosts can enable DEBUG logging for `maf_sandbox_antivirus._tool` to retain escaped, bounded diagnostic text; those logs remain untrusted and must not be added to model prompts. Validated database metadata is in `trusted_output`. The upload-derived input SHA-256 is in labelled `output`, hidden by a FIDES host while remaining available to the host for byte binding. Hosts without FIDES must keep that field out of model prompts themselves.

`clean` means no detection under the configured policy, not proof that a file is harmless or that every possible file format was fully interpreted. Encrypted-content, broken-executable and scan-limit alerts are refusals. Invalid arguments, unavailable updates, invalid signatures, timeouts and engine failures return `completed=False` without a verdict. A stale or engine-incompatible database is refused. All acquired guests are disposed on success, refusal, failure and cancellation.

| Host option | Default | Ceiling |
|---|---|---|
| `exec_timeout_seconds` | 60 | 300 seconds |
| `update_timeout_seconds` | 120 | 600 seconds, including provider and staging |
| `max_bytes` | 32 MiB | 128 MiB; backend admission may impose less |
| `max_database_age_days` | 7 | 30 days, measured against host UTC using the daily database |
| `max_filesize` | 32 MiB | 128 MiB |
| `max_scansize` | 128 MiB | 512 MiB |
| `max_files` | 1,000 | 10,000 |
| `max_recursion` | 16 | 100 |

All limits must be positive; non-finite values and booleans are rejected. The age ceiling uses authenticated database metadata; ClamAV's fixed seven-day advisory does not override an explicitly larger ceiling. `file_store_provenance` and `requires_file_integrity` have core's usual meaning. Use backend CPU, memory, process and disk limits as well; ClamAV's official databases need substantial memory. Start Docker qualification with 4 GiB and measure your deployment.

ZIP members undergo bounded expansion before ClamAV because native archive limits can skip some members without an alert. This guard applies the file count, member size, total expansion and recursion ceilings, and refuses encrypted ZIP entries. Other formats use ClamAV's support and alerts; absence of an alert does not establish complete format coverage.

See the [kind contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/antivirus.md) and [image guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/antivirus-sandbox/README.md).
