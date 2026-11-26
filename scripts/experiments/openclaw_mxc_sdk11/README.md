# OpenClaw MXC SDK 1.1 migration proposal

> Prepared contribution for OpenClaw Core migration [#167292](https://github.com/openclaw/openclaw/pull/167292), based on commit `0cbf9b5481de42e1ea4207c1b27fd50441e51c0b`. This is the active proposal under [#2104](https://github.com/sokolaidev/maf-extensions/issues/2104); it has not been submitted upstream.

[proposal.patch](proposal.patch) updates the SDK and generated lockfile to 1.1.0, requires a successful BaseContainer host result at startup, removes the retired DACL preparation path, and updates the operator guidance and regression tests. The launcher and its command lifecycle tests are unchanged from the migration base. Commands go directly to native creation, which owns request-policy enforcement.

The proposal adds no per-command preflight process, request/native fingerprint records or configuration fields. Existing debug behavior is unchanged. The [earlier preflight experiment](../openclaw_mxc_preflight/README.md) is retained as historical research and is not an adoption prerequisite.

## Apply and verify

Apply only to the pinned upstream checkout:

```sh
git -c core.autocrlf=false apply --check /path/to/maf-extensions/scripts/experiments/openclaw_mxc_sdk11/proposal.patch
git -c core.autocrlf=false apply /path/to/maf-extensions/scripts/experiments/openclaw_mxc_sdk11/proposal.patch
pnpm install --frozen-lockfile --ignore-scripts
pnpm test:extension mxc --maxWorkers=1 --exclude extensions/mxc/test/mxc-sdk-wire-contract.integration.test.ts
pnpm tsgo:extensions
node /path/to/maf-extensions/scripts/experiments/openclaw_mxc_sdk11/verify.mjs extensions/mxc/src/mxc-spawn-launcher.mjs
```

Use the upstream-pinned Node and pnpm requirements in an isolated checkout. The native wire-contract cases are excluded deliberately; this command does not qualify MXC execution.

[verify.mjs](verify.mjs) copies the hash-pinned unchanged launcher into a temporary fixture with an inert SDK and synthetic native files. It exercises the startup probe and both plain/PTY direct creation: success preserves the exit status; creation failure is visible and performs no second creation or per-command probe. Its fixture JSON is temporary and removed outside the experiment directory. It does not prove native enforcement, timeout/cancellation of native creation or production cleanup.

## Remaining qualification

Unsupported hosts lose MXC availability; BaseContainer is not an LPAC-equivalence claim. An arbitrary native override is not qualified by startup success. Native executor/DLL provenance, actual unsupported-request refusal, permitted workload execution, native cancellation/timeout/descendant cleanup, Gateway/model execution and installed upgrade remain separate checks. The [research record](../../../docs/sandbox/research/openclaw-integration.md#migration-only-contribution-scope-2026-11-26) records the decision, exact evidence and unposted coordination draft.
