# Historical OpenClaw MXC preflight experiment

> Historical prototype retained with its original patch and replay hashes. The active contribution is the [SDK 1.1 migration proposal](../openclaw_mxc_sdk11/README.md).

The maintainer removed per-command preflight and request/native fingerprint diagnostics from the proposed upstream contribution on 2026-11-26. MXC 1.1 owns native creation-time enforcement; an additional incomplete probe is not an adoption requirement. Do not combine this patch with the migration-only patch or cite its replay results as verification of the narrower contribution.

[proposal.patch](proposal.patch) and [verify.mjs](verify.mjs) remain unchanged to preserve the earlier research and hosted evidence. The [scope decision](../../../docs/sandbox/research/openclaw-integration.md#migration-only-contribution-scope-2026-11-26) supersedes the earlier submission draft.
