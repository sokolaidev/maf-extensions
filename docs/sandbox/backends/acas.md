# `acas` — ACA Sandboxes

> The reference conformant backend: hardware-isolated micro-VMs on Azure Container Apps, the only one that can enumerate, and the only one whose isolation rung the router's default floor already admits. Install and configuration: [`packages/maf-sandbox-acas/README.md`](../../../packages/maf-sandbox-acas/README.md).

## What it declares

| Declaration | Value |
|---|---|
| `isolation` | `Isolation.MICROVM` |
| `capabilities` | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `HOST_TOOLS` |
| `egress` | `Egress.ALLOWLIST` |
| `limits` | 32 MiB per file, 128 MiB total, 128 files — the same `TransferLimits` in each direction |

The byte ceilings sit well under what a streaming backend could offer, because this one cannot stream: the SDK's `read_file` buffers the whole response, so a per-file ceiling bounds **host memory** rather than transfer cost. `max_files` is comparatively high because a `FILES_LIST` kind fetches each file in a round trip of its own.

## The allowlist is built from the spec

`egress` is `ALLOWLIST` unconditionally, and it is true because the create builds it: `default_action: "Deny"` plus exactly one `Allow` host rule per name in `spec.egress_allow`. Nothing in this backend's configuration widens that list, and a spec that names no hosts gets Deny with nothing allowed — the closed configuration, not the open one. The axis and what a host may state on it are in [`../network.md`](../network.md).

## The reference conformant backend

This is the backend the micro-VM standard was written against, and it meets all four legs: a hardware virtualization boundary; no ambient identity reachable from inside; confinable egress, declared `ALLOWLIST`; and an explicit guest↔host surface. It is remote into the bargain, which is more than the standard asks. What that role means in practice is that it defines what the file surface should *look* like — it is the only backend that can serve `FILES_LIST` — while [`docker`](docker.md) decides whether the surface actually works, because Docker is the one a pull request can run. Keeping reference and gate apart is what stops the richest backend from quietly setting requirements the portable ones cannot meet. The standard itself is in [`../policy-isolation.md`](../policy-isolation.md).

## `HOST_TOOLS` rests on a live measurement

`HOST_TOOLS` is the one capability with no method behind it. What it asserts is that **`exec` detaches** — a process started by one call outlives it and is observable from the next, because the sandbox is a micro-VM the group keeps between calls rather than a session torn down per dispatch. That is what host-tool dispatch is built on: the launcher returns at once and the exit-code file is the run's only witness. Here it could not be taken on faith, since every call is an HTTP round trip to a remote control plane, so `test_acas_e2e.py` measures detachment against the real service rather than reading it off the SDK. It is **not** a claim about the image: the shipped launcher wants `sh`, `nohup`, `printf`, `mv`, `mkdir`, `rm` and `kill`, and a kind wants whatever interpreter it names — none of which this backend chooses, since the spec's image does.

## The pull surface is read past the SDK

Confinement depends on being able to tell a symlink from a regular file, and the SDK's typed `FileInfo` **drops both** of the fields that would say so. The backend therefore reads the data plane's raw stat and list payloads itself, for `isSymlink` and `isDir`, and a payload that omits either flag is **refused** — `AcasEntryPayloadIncomplete`, never read as a regular file — because those two booleans are the whole of this backend's symlink refusal and a service that stops sending them has to break the read loudly rather than degrade confinement to nothing. `mode` is not consulted: it carries permission bits with the type bits stripped. That the reference backend's defence rests on an undocumented preview shape it reaches past its own SDK for is acceptable only stated plainly, and it is filed as [#136](https://github.com/sokolaidev/maf-extensions/issues/136).

A symlinked *parent* is invisible in the final entry's stat, so every one of `stat_file`, `read_file`, `list_dir` and `remove` first walks the components from the filesystem root down through `maf_sandbox.paths.refuse_symlinked_parents`, over this backend's own **unconfined, no-follow** stat. Only a link is a confinement failure there; any other non-directory is an ordinary `ENOTDIR`.

## Enumeration, and what a listing is allowed to say

`list_dir` is native here, and this is the only backend that declares `FILES_LIST`. Three rules keep the response honest, and each of them refuses rather than degrades. The `entries` key is **not defaulted to empty**: the service sends an explicit empty list for an empty directory, so an absent or renamed key means the payload shape changed, and defaulting would hide every output behind a listing that looks legitimately empty. Every listed entry is confined to the working directory *and* required to be a **direct child** of the path that was listed, because the protocol's listing enumerates one level — a sibling or a grandchild in the response is a payload this backend cannot read, not a path a caller asked to traverse. And each entry is classified by the same two flags a stat is, so an entry the service describes incompletely breaks the listing rather than arriving as a regular file.

## Regularity is not provable here

Refusing a symlink narrows an entry to *not a link and not a directory*. It does not prove a regular file, and FIFOs, sockets and device nodes are none of the three. **On ACAS, `EntryKind.FILE` means "not a directory and not a symlink"** — a kind author is entitled to know that, because a FIFO is reported identically to an empty regular file and reading one **never returns**. Nothing available closes it: `mode` has no type bits, and `exec` with `test -f` would reintroduce the in-image shell dependency the `FILES_LIST` split exists to avoid, on exactly the minimal images where the file API is most useful. So the read is **bounded by `read_timeout_seconds`**, turning an indefinite hang into a refusal in the output-error family — a failure no cap or reported size predicted. The missing signal is filed upstream as [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807).

## `FILES_DELETE` is implemented and not declared

`remove` exists, and goes through the data plane's own `delete_file` — no shell, no `rm`. The capability is **withheld** anyway, because the mechanism refuses a link where the protocol says a link is *removed*: the service follows a link in the final component as much as in the parents, an HTTP `DELETE` promises nothing else, and removing one could unlink whatever the guest pointed at. A capability is a promise, and this one cannot be kept while the service's behaviour on a link is unmeasured. `conformance.measure_files_delete_probes` exists for exactly that deadlock — it runs every probe with **no declaration gate and no verdict**, so a mechanism can be measured before anything declares it, since every gated conformance entry point refuses an undeclared subject. The live suite runs the measurement and classifies each probe into expected-pass or expected-fail, so an unclassified finding cannot sit green. One finding has already been fixed rather than classified: the service **accepted an empty directory without `recursive`** while refusing a non-empty one, and the backend now refuses a directory on its kind whatever it holds, because a backend that cannot enumerate cannot tell empty from full. What each capability obligates is in [`../capabilities.md`](../capabilities.md).

## Lifecycle

**Warm resume over cold create.** Every acquire tries the registered sandbox first and waits up to 120 seconds for it to come back from suspension; a sandbox reclaimed by its auto-delete timer between rounds is the expected path, logged at INFO rather than warned, because it means the next call pays for a cold create and the reason is worth a line. **Labels are written at create** — scope, thread, agent, kind, plus the spec's own — and the sandbox is registered immediately after the create returns, before the lifecycle policy is configured, so it stays reachable by purge even if configuration fails. **`dispose_scope` selects from the service by label**, not from process memory, because a conversation delete lands on whichever replica serves it. Label values pass through when short and safe and become a `sha256-` digest otherwise — never a truncation, since two scopes sharing a prefix would let one conversation's purge delete another's sandboxes — and the same mapping runs on write and on query. **Get-or-create is serialised per key**, per running event loop, because a create names no sandbox and the service therefore has nothing to recognise a duplicate by; unserialised, both racing acquires get a running, billable sandbox and only one stays registered.

**The control-plane credential never enters the guest.** `DefaultAzureCredential` lives with the group client in the host process; nothing in the sandbox can reach it, the host's identity, or another conversation's sandbox. That is the standard's second leg, and it is why this backend can claim the rung.

## Status

| Decision | State | Tracking |
|---|---|---|
| The backend, its four declarations, and `FILES_OUT` served natively | shipped | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) open as the `FILES_OUT` tracking issue; the ACAS item landed |
| Reads the raw stat payload because the SDK's `FileInfo` drops `isSymlink` | open, upstream | [#136](https://github.com/sokolaidev/maf-extensions/issues/136) open |
| `EntryKind.FILE` means not-a-directory-and-not-a-symlink; a FIFO read is bounded rather than refused | open, upstream — no signal available to close it | upstream [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807) open |
| ACAS declares `FILES_DELETE` | open — the mechanism refuses a link where the protocol removes it; measured without a verdict in the live suite | untracked |
| A directory is refused without `recursive` whatever it holds — the service accepted an empty one | shipped | [#474](https://github.com/sokolaidev/maf-extensions/pull/474) merged |
| The in-sandbox isolation probe suite that would give the micro-VM claim teeth | parked | untracked — nearest adjacent is [#402](https://github.com/sokolaidev/maf-extensions/issues/402) (open), shared egress probes |
| A guest-platform axis a kind can declare and match | open | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) open |
