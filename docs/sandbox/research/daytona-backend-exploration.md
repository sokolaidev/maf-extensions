# Daytona as a backend: what it would be entitled to claim

> An exploration, not a proposal: whether the suite should grow a `maf-sandbox-daytona` backend, and what such a package could honestly declare. Read against [`../backends/writing-a-backend.md`](../backends/writing-a-backend.md), [`../policy-isolation.md`](../policy-isolation.md), [`../network.md`](../network.md) and [`../capabilities.md`](../capabilities.md). Nothing is decided here and no package exists; the sibling record for the Docker decision is [`docker-backend-exploration.md`](docker-backend-exploration.md).

Read on 2026-09-16 from four sources, and it matters which: the documentation corpus at `www.daytona.io/docs`, both published OpenAPI documents (the control plane and the Toolbox API the guest daemon serves), the Python SDK's published reference, and the daemon's own Go source at tag **v0.190.0**. Nothing was run against a live account. Every claim below is a document read or a source read, and the probe list at the end is what an account would settle.

## Why ask at all

The suite's front door ([`../README.md`](../README.md)) already names Daytona, among ACA Sandboxes, E2B and Modal, as a *sandbox service* — "someone else's machine" — and makes a claim about that whole class: they belong **beneath** this protocol as backends rather than beside it as alternatives. LangChain's Deep Agents ships Daytona as one of its own sandbox providers, and [`maf-sandbox-deepagents`](../../../packages/maf-sandbox-deepagents/README.md) points the other way through the same layer. So Daytona is the nearest available test of the front door's claim, and the first candidate that is neither Azure nor a local engine. It would be the suite's second remote backend and its first non-Microsoft one.

## The governance fact, first, because it dates everything below

The `daytonaio/daytona` repository's README opens with a maintenance notice: *"This repository is no longer maintained. As of June 2026, Daytona's core development has moved to a private codebase."* The last substantive commit is 2026-06-23 and the last tag is v0.190.0, whose daemon carries an `SPDX-License-Identifier: AGPL-3.0` header. The Python SDK is a separate, live artifact: `daytona` on PyPI is Apache-2.0 and shipped 0.214.0 on 2026-09-15.

Two consequences. The service is closed, which by itself is no objection — ACA Sandboxes is closed too, and is this suite's reference backend at `microvm`. The difference is that Daytona *was* readable and no longer is, so the source evidence in this record is **frozen at v0.190.0 while the service runs something newer** — the API exposes a `daemonVersion` per sandbox, which is the field that would say how much newer. A backend's declarations about its file plane would therefore rest on a vendor statement plus a live probe, and the probe would have to be re-run, not read once.

## Where it would sit on the ladder

`SandboxClass` in the control-plane schema is `linux-vm`, `container`, `android`, `windows`, and the isolation page describes the two that matter:

| Class | What the vendor says | Rung it could claim |
|---|---|---|
| `container` | "Isolated container with dedicated namespaces and enforced resource limits. **Code runs as root inside the sandbox** without affecting the runner." | `CONTAINER` — the architecture page confirms the mechanism: "each sandbox runs as an isolated instance with its own Linux namespaces for processes, network, filesystem mounts, and inter-process communication" |
| `linux-vm` | "Full virtual machine with its own kernel. The hardware virtualization boundary enables VM-only capabilities: pause / resume, fork, and hot snapshots." | `VM` is the candidate — a dedicated full guest on remote infrastructure — subject to all four micro-VM conditions |

The marketing line "a dedicated kernel" appears on the front page and in the repository README; the architecture page contradicts it for the container class, and the class table is the honest text. A backend must not round that up.

**The class is a property of the snapshot, not of the create call.** `CreateSnapshot` carries `sandboxClass`; `CreateSandbox` does not, and a VM sandbox is made by creating a snapshot with `sandbox_class=SandboxClass.LINUX_VM` and then creating from it. VM sandboxes "can currently only be created from existing VM snapshots", with no declarative build path. That is a good fit for the rule the ladder needs: `isolation` is a constant of the backend *instance*, so a `DaytonaSandboxConfig` pinned to one snapshot family declares one rung, and a deployment wanting both registers two instances — the same shape settled as Decision 5 in [`../guest-platform-and-commands.md`](../guest-platform-and-commands.md).

Against the four conditions, for the `linux-vm` class: **(1)** the vendor claims a hardware virtualization boundary and names no hypervisor, which is weaker evidence than Azure's but is a claim rather than an inference. **(2)** is unsettled and interesting — a sandbox holds an auth token the control plane resolves, and `GET /organizations/sandbox-identity/by-sandbox-auth-token/{authToken}` returns exactly `{sandboxId, organizationId}`, so the token identifies the guest rather than carrying the host's control-plane credential; `secrets` are injected as opaque placeholders that an outbound proxy substitutes only for the secret's allowed hosts, which is the shape condition 2 wants. Whether anything else reachable from inside the guest widens that is a probe. **(3)** is the tier question below, and it is the one that decides. **(4)** holds only if `volumes` and `linkedSandbox` stay off: a linked sandbox joins a parent and its children into a shared link network, which is a channel between sandboxes and therefore between conversations, and a volume mounted into two sandboxes is shared writable state.

## Egress: the closest fit in the family, and a price tag

The allowlist grammar matches almost exactly. `domainAllowList` is a comma-separated list of DNS domains where "a domain with `*.` allows the base domain and its subdomains" — which is the grammar `SandboxSpec.egress_allow` already validates and refuses everything outside of. `networkBlockAll` is `CLOSED`. Both are create-time arguments to the service, enforced above the guest, so there is no in-guest component to bypass and no proxy container to route around — the ACAS property rather than the docker one, and better than either at the L7 level, since a domain rule needs no CONNECT tunnel to match on.

Then the tier:

> **Tier 1 & Tier 2**: Network access is restricted and cannot be overridden at the sandbox level. Organization-level network restrictions take precedence over sandbox-level settings. Even with `networkAllowList` or `domainAllowList` specified when creating a sandbox, the organization's network restrictions still apply. Essential services remain reachable.
>
> **Tier 3 & Tier 4**: ... A sandbox-level `networkAllowList`, `domainAllowList`, or `networkBlockAll` replaces the default policy for that sandbox. Enforcement is strict: only destinations you list are allowed (or none, when blocking all). Essential services do not bypass a sandbox allow list or block-all setting.

"Essential services" is a documented list of roughly two dozen categories, and it includes `*.blob.core.windows.net`, fifteen S3 regional wildcards, Box, messaging services, LLM observability endpoints, and every major model API — `*.anthropic.com`, `*.openai.com`, `generativelanguage.googleapis.com`, `openrouter.ai`. For a kind running model-written code, that set *is* the exfiltration surface an allowlist exists to close.

So the mode a backend may declare is a property of the **organization's billing tier**, and the tier table prices it: Tier 1 is email verification, Tier 2 is a card and a $25 top-up, and **Tier 3 is a $500 top-up**. Below Tier 3 the honest `egress_modes` is `frozenset()` — silence is the empty set and every ask is refused, including `CLOSED`. Combined with a container-class instance declaring `CONTAINER`, two rungs under the default floor, the honest Tier-1 configuration is a backend that can serve nothing at all. That is the axis working, not a defect, but it means the cheap way in is the way that does not work.

Three more mechanical facts a backend would owe. The allow lists are **mutually exclusive** and a conflicting combination is a `400`, which costs us nothing since `egress_allow` carries hosts only. They are **capped** — 100 domains, 10 CIDRs — so a spec naming more must be refused rather than truncated, because a truncated allowlist is a served run that is not the run that was asked for. And there is no method scoping anywhere in the surface, so `EGRESS_METHODS` and `egress_method_tokens` stay undeclared, as they are on every shipped backend.

One flag looks like the gate to build on: `Organization.sandboxLimitedNetworkEgress`, described only as "Sandbox default network block all". It is readable through the API, so a backend could refuse at construction rather than declare a mode it will not get. What it actually means needs a live read before anything rests on it.

## The file plane runs inside the guest, and that is the finding

From the architecture page: *"The sandbox daemon is a code execution agent that runs inside each sandbox. It exposes the Toolbox API ... file system and Git operations, process and code execution."* There is no host-side file plane. Every `upload_file`, `get_file_info`, `list_files` and `delete_file` is served by a process in the workload's own namespaces — and on the container class, running as root beside a workload that is also root.

Two consequences, and the second is structural.

**Every observation is guest-answered by construction.** The guide's rule — never answer the filesystem path check by running anything inside the guest, because the guest can replace what answers — has a documented escape in `stat_by_asking_the_guest` as a *declared posture*. Daytona would be the first backend where that is not a fallback for a thin engine but the only shape the architecture admits, and it is weaker than the WSLC posture it resembles: WSLC's answer comes from a host binary reading the container's filesystem, and this one comes from a peer process the workload can signal, replace, or race.

**The check-then-act window cannot be closed the way Docker closed it.** [#1130](https://github.com/sokolaidev/maf-extensions/issues/1130) froze the guest with `docker pause` across check and copy. Daytona's pause is VM-class only — and it would freeze the daemon along with the workload, because the daemon is inside the boundary being frozen. There is no state in which the check still holds and the copy can still run. That is a residual to be stated, in the package README and beside the capability declaration, the way [`wslc-write-window.md`](wslc-write-window.md) states WSLC's.

Then the source, at v0.190.0. **Every path observation in the daemon's `fs` package is `os.Stat`, which follows symlinks. There is not one `os.Lstat`.** `getFileInfo` calls `os.Stat`; `ListFiles` reads the directory and calls that same `getFileInfo` per entry; `DeleteFile`, `MoveFile` and `SetFilePermissions` each stat the same way. What follows, probe by probe:

- **`stat_file` cannot report `EntryKind.SYMLINK`.** A link is described as its target, and `FileInfo` carries `name`, `size`, `mode`, `modTime`, `modifiedAt`, `isDir`, `owner`, `group`, `permissions` and no link field. This is [#136](https://github.com/sokolaidev/maf-extensions/issues/136)'s shape on ACAS, and worse: there, the backend reads the raw stat payload the model dropped, and here the follow happens before anything is serialized, so there is no payload to recover it from.
- **`list_dir` cannot name its links** — `a-listing-names-its-links`, and "never hide a link from the listing" is the rule it breaks.
- **`upload_file` writes through a link.** The handler is Gin's `SaveUploadedFile`, which creates the destination; a final component that is a symlink resolves and the bytes land on its target — `a-linked-destination-is-refused-not-followed` fails at the engine, so the refusal must be entirely the backend's, resting on a check the guest answered.
- **`delete_file` gets one rule right and two wrong.** It stats first, so a link *to a directory* is refused without `recursive` and a dangling link answers `404`, while the protocol wants the link unlinked in both cases and treats a missing path as success — a 404 mapped to success would silently leave the link in place. With `recursive=true` it is `os.RemoveAll`, which unlinks a symlink operand rather than following it, so the sharpest rule — `a-link-is-removed-never-followed` — is the one that holds.

A note on the reach rule, which is satisfied here for an uncomfortable reason. On the container class the daemon and the workload are both root, so nothing the file plane lands is beyond what the guest can change, and the REACH probes find nothing to swap and stop rather than fail. The rule permits that. It is also a statement about how little separation exists inside the sandbox.

## exec: one stream, text on the wire, whole seconds

`ExecuteRequest` is `{command, cwd, envs, timeout}` and `ExecuteResponse` is `{exitCode, result}` — two fields. The handler pipes the command into `common.GetShell()` on stdin, sets `Setpgid`, and assigns **one buffer to both `cmd.Stdout` and `cmd.Stderr`**. Each consequence has already been answered somewhere in this repository:

- **No argv form.** The backend quotes a sequence, which the protocol already says is the backend's job and never the caller's.
- **The program gets no stdin**, because the command text is stdin. A kind that pipes input would have to write a file instead.
- **The streams are merged by construction.** The honest options are `ExecResult.producer_owns_stderr` with the program's own stderr routed into `stdout` and nothing of the backend's left there, or the capture wrapper below. Sessions are the third path: `SessionExecuteResponse` carries `stdout` and `stderr` as separate fields. Whether those are genuinely separate pipes or one log split after the fact is not answerable from a schema, and belongs on the probe list.
- **`result` is a JSON string, so bytes do not survive.** Go's `encoding/json` replaces invalid UTF-8, and `exec-byte-fidelity` fails on this endpoint. [`_exec_capture.py`](../../../packages/maf-sandbox-acas/src/maf_sandbox_acas/_exec_capture.py) in the ACAS backend is this exact problem solved once already — a bounded base64 capture over `sh` — and its cost transfers with it: the image must carry `sh mkdir mkfifo head cat wc dd base64 rm rmdir`, and a live test that deletes `sh` is the thing that keeps the claim honest.
- **`timeout` is whole seconds and defaults to 10.** A caller's fractional bound has nowhere to go, and neither rounding is free: down borrows `TimeoutError` for a limit the caller did not set, which the guide forbids outright, and up overruns the budget. The shape that fits is the caller's bound enforced host-side with the service timeout set as a ceiling above it — and whichever way it goes, "a backend must document any separate bounded allowance".
- **Timeout discards the output.** The daemon SIGKILLs the process group and returns `408` without the buffer. Lossless timeout output shipped in [#465](https://github.com/sokolaidev/maf-extensions/issues/465), so the capture wrapper would be doing double duty: bytes written to a guest file survive the kill and can be read back afterwards.

`code_run` exists and runs Python, TypeScript or JavaScript, but `RUN_CODE` should stay undeclared: which runtime a snapshot carries is the snapshot's property, and the guide is explicit that a backend accepting arbitrary images may not declare a capability as a claim about someone else's artifact. A config pinned to one known snapshot could revisit it.

## What it gives that no local backend does

The lifecycle surface is the strongest single argument for the package, and it is the thing docker and wslc structurally cannot offer. `autoStopInterval`, `autoArchiveInterval`, `autoDeleteInterval` and a wall-clock `ttlMinutes` are all enforced by the service, so **a sandbox goes away when the host dies** — where a crashed host leaves containers behind on a local engine and an operator cleanup job to find them. `ephemeral=True` is `autoDeleteInterval=0`: deleted on stop.

`labels` are settable at create and replaceable after, and `GET /sandbox` filters on them server-side, which is exactly what get-or-create and `dispose_scope` need — "purge consults the service, never process memory". Sandboxes are addressable by name as well as id, which is the guide's other suggestion for the acquire race: derive a name the provider rejects duplicates of. Whether it does reject them is a probe, and the answer decides whether the backend needs its own serialization.

`AsyncDaytona` is a real async client with an async context manager, so the backend needs no thread pool — unlike the two subprocess backends. `otelEndpointOverride` per sandbox is a thread worth pulling for `maf-sandbox-otel`.

## What the package would cost

**Dependency weight, measured on 2026-09-16.** `uv pip compile` on `agent-framework-openai>=1.13.0,<2`, `opentelemetry-sdk>=1.44,<2` and `daytona`, at Python 3.12, resolves cleanly: `daytona==0.214.0` with `agent-framework-core==1.18.0` and `opentelemetry-sdk==1.44.0`, 67 pins in total. No conflict — but the SDK brings six generated client packages of its own, plus `obstore` (a compiled extension), `aiohttp`, `httpx`, `httpx-ws` and `python-socketio`. The ACAS backend depends on three Azure packages. That is the comparison to put in front of whoever approves it.

**Code.** The shipped backends' `_backend.py` are 2143 lines (wslc), 2179 (acas) and 3699 (docker). A conformant Daytona backend is that size, plus a capture wrapper, plus the confinement work that cannot lean on an engine stat.

**Release.** [`../../../RELEASING.md`](../../../RELEASING.md) says a brand-new package publishes **last**, after every existing dependent admits the new core, to avoid the incompatibility window.

**Money.** A live leg needs an account with credits, and a truthful `ALLOWLIST` or `CLOSED` declaration needs Tier 3, which is a $500 top-up. The conformance and egress suites both run against a real instance by design, so this is a standing cost, not a one-off.

## What reading could not answer

For an account at Tier 3 or above, in roughly this order:

1. Does `get_file_info` on a symlink describe the link or its target, against the **current** daemon — and does `mode` ever carry Go's `L` prefix? This single answer decides whether `FILES_OUT` and `FILES_LIST` are declarable at all.
2. Does `upload_file` to a path whose final component is a symlink write through it?
3. Does `delete_file` without `recursive` refuse a link to a directory, and what does it answer for a dangling link?
4. Are `SessionExecuteResponse.stdout` and `.stderr` separate pipes, or one log split afterwards?
5. What survives `exec` for a program writing invalid UTF-8 and NUL bytes on both streams — and does the ACAS capture wrapper transplant unchanged?
6. Does `network_block_all` genuinely sever at Tier 3, measured with both controls the egress suite requires: an allowed host reachable and a denied host refused?
7. On a Tier 1 or 2 organization, is an essential-services host reachable from a sandbox created with `network_block_all=True`? This is the claim the tier text implies and does not state.
8. What does `Organization.sandboxLimitedNetworkEgress` actually report, on each tier?
9. Does creating two sandboxes with the same `name` concurrently fail the second one?
10. What is inside the guest that the control plane trusts — the auth token's reach, and whether anything else in the environment widens it.
11. Cold-create and warm-reacquire latency, since the whole point of get-or-create is a fix-round loop that does not pay a create per iteration.

## Verdict, held loosely

There is a real backend here, and it is not the one the pricing page invites you to try. On the `container` class at Tier 1 or 2 the honest declarations are `CONTAINER` with an empty `egress_modes`, which is a backend that serves nothing — so the free path is not a starting point but a dead end. The package that could exist is pinned to a `linux-vm` snapshot family on a Tier 3 organization: a second remote backend with server-enforced lifecycle, server-side label queries, a native domain allowlist that matches our grammar, and a genuinely async client.

What it can never be is a peer of ACAS on the file surface. The file plane lives inside the guest, it follows every symlink it is given, and the freeze that closed the window on Docker is unavailable because the thing to freeze and the thing doing the copying are the same process tree. A Daytona backend would either declare `FILES_OUT` and `FILES_LIST` on top of a guest-answered check with the posture written down where a kind author reads it, or withhold them and serve `EXEC` and `FILES_IN` alone. Both are honest; only the second is cheap.

So: worth doing, after the open confinement work rather than before it, and not as the next backend. The reason to do it is the front door's claim — that these services are backends beneath a contract rather than alternatives beside it — and a service whose file plane fits this contract this badly is the strongest available test of it.
