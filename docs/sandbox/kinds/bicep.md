# The `bicep` kind

> `bicep_validate`: the first sandbox workload, and the template every later kind follows. Its contract, its egress allowlist and why those four hosts, and the pinning that makes a shell template safe. Install and wiring are [`maf-sandbox-bicep`](../../../packages/maf-sandbox-bicep/README.md)'s README; the pattern it set is [`README.md`](README.md).

## The contract

One tool. The model writes Bicep into the agent's file store; `bicep_validate` writes the named files into a sandbox, runs `bicep build` and `bicep lint` there, and returns the compiler's SARIF diagnostics as structured text. T2 — compiler truth — instead of T0, the model reading its own template and agreeing with itself.

| | |
|---|---|
| Kind | `BICEP_KIND = "bicep"` — half of the sandbox's identity, so this workload never shares a sandbox with another |
| Tool | `BICEP_VALIDATE_TOOL_NAME = "bicep_validate"` |
| `requires` | left at the protocol default, `{EXEC, FILES_IN}`. The kind writes files in and runs a compiler; it pulls nothing back, so it asks for no pull surface and runs on every shipped backend, wslc included |
| `egress` | one of `{UNRESTRICTED, ALLOWLIST, CLOSED}` — the set `bicep_sandbox_spec` guards at construction, refusing anything else — defaulting to `ALLOWLIST`, Bicep's designed posture. A deployment that will not use modules lowers it to `CLOSED`; one running on a backend that cannot confine at all raises it to `UNRESTRICTED`. **Only the mode is a deployment's to choose** |
| `egress_allow` | the four hosts below, fixed in the package and carried only on an `ALLOWLIST` run — the payload of the mode, never a second dial |
| `work_dir` | `/maf-sandbox/work`, a path nothing else owns. Not `/tmp`: a tmpfs mounted over `/tmp` would hide the `bicepconfig.json` baked into the image, and that failure looks completely healthy — SARIF still parses, diagnostics still render, against a weaker rule set than the repo asked for |
| `min_isolation` | not raised. The host's floor governs ([`../policy-isolation.md`](../policy-isolation.md)) |
| `declarations` | **empty** — no `source_integrity` and no confidentiality cap. The compiler is first-party and deterministic, but what it is deterministic *about* is a template the model wrote, so the result does not derive from wholly trusted input; the framework's untrusted default is what applies, and it is the honest reading (below). No cap because a host's confidentiality tiers are the host's classification, and declaring one here can activate a policy leg a given host keeps dormant. `TestFidesDeclarations` pins the resulting dict |

## The diagnostics are not trusted input, and saying so costs something

The rule this section applies, and what a kind may claim in general, is [`../information-flow.md`](../information-flow.md).

The environment argument for `"trusted"` is not the question, and it is not uniformly true either. On the default `ALLOWLIST` run every clause of it holds — the compiler is Microsoft's, the sandbox carries no ambient identity, nothing is reachable but the four restore hosts. The other two postures only weaken it: `egress_allow` is `()` off an `ALLOWLIST` run, so `UNRESTRICTED` — what a deployment raises to on a backend that cannot confine at all — reaches whatever the host can, and `CLOSED` takes the registry away and leaves the template behind. None of that moves the conclusion, because what the compiler is deterministic *about* is the model's own Bicep, and that reaches the rendered result in two places. The **message** is passed through verbatim, and Bicep quotes the source in it — `BCP057` is "The name … does not exist in the current context", where the name is whatever the model wrote. The **file name** is the model's `files` argument, rendered into every location: `[warning] BCP035 @ main.bicep:31: …` names a string the model chose.

So a `"trusted"` declaration would not be a hint the framework reconciles against what it already knows. FIDES treats a declared level as an **override**: it discards the input-label join rather than flooring it, which means declaring `"trusted"` instructs a host's middleware to disregard the input side entirely. Undeclared, the untrusted default applies and the fail-safe direction is the one that holds.

**What it costs a FIDES host, and the two costs are alternatives rather than a pair.** With `auto_hide_untrusted` — the framework's default — the whole result is replaced by a variable reference, and hidden items do not taint, so the conversation label is unchanged and later tools run ungated. The model then cannot read the diagnostics, which is the entire purpose of the tool. Turn hiding off and the diagnostics are visible, the conversation goes untrusted, and `PolicyEnforcementFunctionMiddleware` gates every later tool that has not opted in through `allow_untrusted_tools` or an `accepts_untrusted` property. A host gets one or the other.

Nothing in this kind splits the difference: it answers with one string under one label, so it cannot keep a host-generated summary visible while hiding the compiler's own text. Per-item labels are the road that would ([#803](https://github.com/sokolaidev/maf-extensions/issues/803)), and they need the tool's return type widened past `str` in core.

## Four egress hosts, in two pairs

The allowlist is a property of the workload and lives in the package, not in configuration: a deployment able to widen Bicep's egress could undo the containment the whole design rests on. It is the **payload of an `ALLOWLIST` run** — the mode is the deployment's choice, the hosts are the kind's — and on that run everything unlisted is denied, ARM above all, which a `ts:` reference would otherwise dial with the host's credentials.

| Host | Why |
|---|---|
| `mcr.microsoft.com` | AVM (`br/public:`) manifests |
| `*.data.mcr.microsoft.com` | the layer blobs. With only the first allowed, restore resolves the manifest and then 403s on the blob — BCP192 on every `br/public:` reference — so module types never load and type errors in module inputs become structurally invisible |
| `aka.ms` | the public module *index* is fetched from a hard-coded `aka.ms` URL |
| `live-data.bicep.azure.com` | what that URL redirects to. Both hops must be allowed: the redirector alone answers with a `Location` pointing at a host that is still denied |

The index fetch belongs to restore rather than to the analyzer — deliberately, so lint rules never download during analysis — so it is attempted on every `build` and every `lint` whatever rules are enabled, and the only switch that stops it, `--no-restore`, is the one that would cost the module types.

**A blocked restore does not go quiet, it goes misleading**, which is why the tool has a banner for it. `use-recent-module-versions` reports "Could not download available module versions" once per file — a warning that reads like a finding about the source while the check it stands for never runs. An agent can, and once did, discount exactly that noise as environment trouble and certify module inputs from READMEs instead of from the compiler. So a run with any BCP190/BCP191/BCP192 returns a `MODULE RESTORE FAILED` header ahead of the diagnostics, saying type checking did not run and the validation is incomplete, rather than a diagnostic list that reads healthy. All four hosts are Microsoft-operated, and the containment posture — no ARM, no ambient identity, nothing reachable that could carry the host's credentials — is unchanged by any of them.

## Running `CLOSED`, on purpose

A deployment that will not use AVM modules builds the spec with `egress=Egress.CLOSED`, and the run is served at `CLOSED` on any backend that can cut the network. [`samples/05_docker_bicep`](../../../samples/05_docker_bicep) is exactly that case: a module-free template compiled on `--network none`, completing fully offline with nothing to report. The posture is **stated rather than inferred from the host list**, which is what the old model could not do: a backend that cut the network entirely confined *more* than the four hosts asked, so the run went through on a warning naming what would be unreachable, and "offline on purpose" and "offline by accident" read identically. A template that then *does* reference a module fails inside the sandbox and the banner above reports the shortfall, which is a template/posture mismatch surfaced loudly at run time rather than a router quietly serving less egress than the spec named.

## Templates, and the pin that makes them safe

Three shell templates, module-level constants, with exactly one substitution each:

```
bicep build       {path} --diagnostics-format sarif 2>&1 || true
bicep build-params {path} --diagnostics-format sarif --outfile /dev/null 2>&1 || true
bicep lint        {path} --diagnostics-format sarif || true
```

`{path}` is reached only after `resolve_listed_path` has cleared the name twice: against the caller's file store listing, and against resolving inside the call's own directory. A name outside `[A-Za-z0-9._/-]`, or holding a `..` segment, is refused with **no listing echoed back** — echoing one would invite a retry with another spelling. A name that is merely absent from the listing gets the near misses, because that is a wiring problem rather than an attack. And the *listing's* key is what the store is then read by, not the caller's spelling: `./main.bicep` validates but would not read back from a store keyed `main.bicep`.

The listing itself is the boundary, so **failing to enumerate is a refusal, not an empty list** — that rule lives in the session, and this kind returns its message unchanged.

Two orderings inside the call are load-bearing and easy to get wrong in the obvious rewrite. Every file is written before *any* is compiled, because Bicep resolves `module` and a parameter file's `using` off the filesystem at compile time — writing and compiling one at a time reports "module not found" for perfectly good templates, and for a `.bicepparam` it is wrong about half the time. And each call gets a **fresh** directory rather than a wiped one: the sandbox is reused across fix rounds, `bicepconfig.json` sits at the work-dir root, and a recursive delete would take the repo's lint rules with it. Fresh directories make staleness impossible by construction; the call owns the path and the framework reclaims it ([`../tool-call.md`](../tool-call.md)).

## Sanitized surfaces

Every failure path returns a fixed sentence naming the file and the phase, and sends `error_detail(exc)` to this module's logger: a store read that raises, a listed file with no content, a `write_file` that comes back `Conflict`, an exec timeout, an exec failure, unparseable SARIF. The reason is not tidiness. A live run produced `Operation returned an invalid status 'Conflict'` for four files at once, and that sentence alone cannot tell "the directory already exists" from "the sandbox is suspending" — the difference between a bug and a retry. The log needs it; the transcript must not have it.

## The CLI behaviours live in the source

Three hard-won facts about the pinned Bicep CLI are documented where they bite, in [`_tool.py`](../../../packages/maf-sandbox-bicep/src/maf_sandbox_bicep/_tool.py), and that is their home rather than this page:

- `bicep build` emits SARIF on **stderr** while `bicep lint` emits it on **stdout**, hence the `2>&1` on one leg and not the other. The two phases share `_run_phase` for this reason — writing them twice is how the build leg's `2>&1` came to be missing once already.
- `.bicepparam` is a parameter file, not a template, and `build` refuses it in prose that is not SARIF; `build-params` is the counterpart, with `--outfile /dev/null` because only the diagnostics are wanted.
- `bicepconfig.json` is found **only** by walking up from the source file — the pinned CLI has no `--config-file` on either command — which is why the config sits at the work-dir root and why a per-call subdirectory still picks it up. `TestConfigDiscovery` pins the image against that constant, and CI checks the built image actually contains the file.

## No isolation raise

`bicep_sandbox_spec` sets no `min_isolation`, and that is an answer rather than an omission: the workload compiles text the model wrote against Microsoft-operated endpoints, and how strong the boundary must be *here* is the host's policy. A spec may only raise the floor, never lower it, so a kind that raised one would be overriding a deployment that knows more about its own exposure than the package does.

## Status

| Decision | State | Tracking |
|---|---|---|
| `bicep_validate` as the first kind: fixed templates, listing-pinned paths, sanitized surfaces, zero Azure imports | shipped | — |
| Four AVM egress hosts fixed in the spec rather than in configuration | shipped | — |
| The factory takes an `egress` mode, guards `{UNRESTRICTED, ALLOWLIST, CLOSED}` at construction and defaults to `ALLOWLIST`; the hosts stay the kind's | shipped | [#525](https://github.com/sokolaidev/maf-extensions/issues/525) (open, the per-kind record) delivered in [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| A module-free template compiles on a `CLOSED` run, and the mismatch is reported at run time rather than resolved at attach | shipped — [`samples/05_docker_bicep`](../../../samples/05_docker_bicep) is the worked case | [#534](https://github.com/sokolaidev/maf-extensions/pull/534) (merged) |
| The `MODULE RESTORE FAILED` banner ahead of a restore-blocked diagnostic list | shipped | — |
| A fresh call directory per call, reclaimed by the framework | shipped | [#496](https://github.com/sokolaidev/maf-extensions/pull/496), kinds wired in [#500](https://github.com/sokolaidev/maf-extensions/pull/500) |
| `requires` left at `{EXEC, FILES_IN}`; no `min_isolation` raise | shipped | — |
| The diagnostics carry the model's own identifiers and file names, so the tool declares no `source_integrity` | shipped — and a FIDES host now either hides the diagnostics from the model or lets them taint the conversation, with per-item labels the road out of that choice | [#801](https://github.com/sokolaidev/maf-extensions/issues/801) (closed), under [#774](https://github.com/sokolaidev/maf-extensions/issues/774) (open); the labels are [#803](https://github.com/sokolaidev/maf-extensions/issues/803) (open) |
| A guest-OS axis — this kind needs a `bicep` binary on the path, and nothing declares it | shipped in core, unused here — `requires_os_family` exists and this spec leaves it `None`, which asks nothing and is refused by nothing. The binary itself stays outside the axis: what an image carries is the image's property, not the guest's shape ([`../guest-platform-and-commands.md`](../guest-platform-and-commands.md)) | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged) |
| The result splits, each item labelled on its own derivation: standing guidance stays visible while the diagnostics and their count are untrusted | not shipped — a kind body returns `str`, which is one item under one label | the mechanism is [#803](https://github.com/sokolaidev/maf-extensions/issues/803) (open), which scopes itself to widening the core return type; **this kind's adoption of it is `untracked`** and wants its own issue once the mechanism lands |
