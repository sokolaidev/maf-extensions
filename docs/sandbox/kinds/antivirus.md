# Antivirus

The antivirus kind scans a quarantined binary upload with a fixed ClamAV program. It requires POSIX `EXEC` and `FILES_IN`, container isolation or stronger, call isolation and disposal. Guest egress is closed throughout database preparation and scanning; no `FILES_OUT` capability or output sink is needed.

The image supplies an authenticated seed. A host-owned provider selects immutable signed CDIFF chains or signed CVD replacements for that exact seed. The guest verifies and applies them before receiving the upload. Missing chains can use full replacements within the transfer budget; failures never silently fall back to an older seed. Hosts own mirror refresh, caching and snapshot retention. Image releases update the engine, OS and seed without a daily rebuild requirement.

Freshness is the age of the authenticated daily database measured against host UTC, not image build or download time. Database versions, functionality levels and timestamps are validated before scanning; incompatible engines and stale databases are refused. Completed results distinguish `clean`, `infected` and `refused`; operational failures have no verdict. Completion and verdict are wrapper-owned. Signature names and guest diagnostics are excluded from tool results and retained only as bounded, escaped host DEBUG logs; hosts must keep these untrusted logs out of model prompts. A clean result does not change the input's integrity label or guarantee universal format coverage.

The host gates readers and promotes exactly the scanned bytes, identified by the host-computed SHA-256. Scanning a name and later reading changed bytes does not establish admission. Retain parser guards even after a clean scan. The [package guide](../../../packages/maf-sandbox-antivirus/README.md) defines options, bounded transfers and direct host calls; the [image guide](../../../images/antivirus-sandbox/README.md) defines build and qualification procedures.

## Status

| Decision | State | Tracking |
|---|---|---|
| Offline scanning with a baked seed and host-supplied signed updates | Implemented; release and live ACAS qualification pending | [#2325](https://github.com/sokolaidev/maf-extensions/issues/2325) (closed) by [#2330](https://github.com/sokolaidev/maf-extensions/pull/2330) (merged) |
