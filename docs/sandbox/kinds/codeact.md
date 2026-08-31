# The `codeact` kind

> `execute_code`: the model writes a short Python program, the program runs in a sandbox, and the tool returns what it printed. Its contract, how `requires` is assembled from the channels a host wired, why an empty registry is the security story, and the `RUN_CODE` road it does not take. Install and wiring are [`maf-sandbox-codeact`](../../../packages/maf-sandbox-codeact/README.md)'s README; the pattern it inherits is [`README.md`](README.md).

This kind has no design document of its own. Its hypothetical predecessor is the worked example in [`../research/two-axis-sandbox-policy.md`](../research/two-axis-sandbox-policy.md) § *A CodeAct kind on ACA Sandboxes*, written to show every policy axis doing work before any of it was built; what shipped differs from it in shape, and the code is what governs.

## The contract

| | |
|---|---|
| Kind | `CODEACT_KIND = "codeact"` |
| Tool | `EXECUTE_CODE_TOOL_NAME = "execute_code"` |
| `requires` | assembled, not declared — see below |
| `egress` | **derived, never passed**: the union of the kind's hosts and the deployment's, non-empty, runs `ALLOWLIST`; empty runs `CLOSED`. `UNRESTRICTED` is not expressible here |
| `egress_allow` | `()` from the kind, unioned with whatever the deployment adds — the payload of the `ALLOWLIST` run the union derives |
| `work_dir` | `/maf-sandbox/work`, a dedicated root rather than the image's own tree |
| `min_isolation` | not raised. This kind runs only what the model wrote, so the host's floor governs ([`../policy-isolation.md`](../policy-isolation.md)) |
| `requires_os_family` | not set. The axis exists and this kind does not use it, so nothing refuses a guest here — even though the program is run with `python3` and host-tool calling additionally needs `sh` and `nohup`, which are properties of the image rather than of the guest's shape |
| `source_integrity` | **undeclared**, on purpose. The library's `"trusted"` default is right for a compiler's diagnostics and wrong here: what comes back is whatever a model-written `print(...)` chose to emit. Undeclared, the framework's untrusted default applies and the result taints the conversation — the fail-safe direction. `withhold_guest_output=True` inverts it by removing the premise: with the streams withheld the result holds no guest-authored text, so the tool declares `"trusted"` — unless a wired registry's `result_integrity` reads `untrusted`, which still takes it away |

Four channels exist and none is on by default: a file store, an output sink, a host-tool registry, and an egress allowlist. Wire none and this is the stdout-only kind it has always been. The tool's *signature* follows what was wired — `files` appears only with a store, `outputs` only under `CodeactOutputs.DECLARED` — so a model is never shown a parameter this deployment cannot honour.

## `requires` is assembled from the wired channels

`_codeact_spec` builds the set rather than stating one, at [`_tool.py:481-489`](../../../packages/maf-sandbox-codeact/src/maf_sandbox_codeact/_tool.py):

```python
collects = outputs is not CodeactOutputs.NONE
requires = {Capability.EXEC, Capability.FILES_IN}
if collects:
    requires.add(Capability.FILES_OUT)
if surface is not None:
    requires |= {Capability.HOST_TOOLS, Capability.FILES_OUT}
```

| Wired | Adds | Why |
|---|---|---|
| nothing | `{EXEC, FILES_IN}` | `FILES_IN` even with no file store: the kind writes `program.py` into the guest, so the source reaches the interpreter as file *content* and never as a command line |
| an output mode (`DECLARED` or `MANIFEST`) | `FILES_OUT` | and `outputs_named_at_call_time`, which is what keeps the attached tool honest about landing artifacts it cannot yet name. Never `FILES_LIST` — both roads collect literal paths and neither enumerates, so the kind runs on every backend serving the pull surface rather than only the one with the richest file API |
| a non-empty host-tool registry | `HOST_TOOLS` **and** `FILES_OUT` | the second is for the *transport*, not for this kind's outputs: `host_tool_calls_over_exec` stats and reads its own request files and the exit marker back over the pull surface, so even a stdout-only program that can call a host function needs it |

Two consequences worth stating plainly. A registry drops wslc twice over — it declares only `{EXEC, FILES_IN}` — and the refusal happens where the tool would have been built, so `make_codeact_tools` raises `SandboxCapabilityNotSupported` rather than leaving a wiring that fails at the first call. And a requirement the capability set *cannot* express: the launcher `host_tool_calls_over_exec` writes is POSIX shell and needs a guest with `sh` and `nohup`, so a distroless or Windows image is out whatever it declares — which is the gap [`../guest-platform-and-commands.md`](../guest-platform-and-commands.md) settles and [#111](https://github.com/sokolaidev/maf-extensions/issues/111) closed one half of: `requires_os_family` now exists for a kind to declare a guest's *shape*, and this kind sets none, because none of `python3`, `sh` or `nohup` is a question about shape — what an image carries is the image's property. `python3` is the `RUN_CODE` road rather than a declaration, and `sh` and `nohup` are infrastructure commands owed protocol methods of their own. The spec also carries the registry's `identities`, so a router's `denied_identities` can refuse the widened spec at attach. **Reading a registry seals it**, so ask for the spec once everything is registered.

## Egress: a derived mode, closed by default

The spec carries **one mode**, and this kind computes it rather than accepting it: `_effective_egress` unions what the kind needs with what the deployment added, and `egress = Egress.ALLOWLIST if effective_egress else Egress.CLOSED` (`_tool.py:495-496`). Named hosts run `ALLOWLIST` with those hosts as the payload; no hosts at all runs `CLOSED`, which is what a caller that says nothing gets — the program computes and cannot fetch.

**`UNRESTRICTED` is not expressible, and that is the design rather than an omission.** This sandbox runs model-written code, and unconfined model-written code reaching anything is the exfiltration case an allowlist exists to prevent. There is no argument to pass and no host list that produces it: the derivation has two outcomes and neither is the open posture. Where [`bicep`](bicep.md) takes a mode as an argument — a fixed compiler is low-risk unconfined, and the in-process dev sample needs it — this kind refuses to offer one.

The union has two halves and the spec carries both, because the union is what the router matches against the backend and what decides whether the tool is declared as carrying something out — either half alone would understate the sandbox.

- **The kind's half is empty and fixed in the package** (`_KIND_EGRESS = ()`). Nothing in `execute_code` resolves a module or installs anything, so there is nothing for it to need; fixed rather than configurable because a deployment able to widen what the *kind itself* requires could undo the containment. Empty is an answer here, not an omission.
- **The deployment's half is `make_codeact_tools(egress_allow=…)`**, for endpoints a published kind cannot know — a package index, an internal artifact store.

Naming a host is therefore two things at once: it adds the host, and it moves the run from `CLOSED` to `ALLOWLIST`. The code treats that as the widening it is. Every allowed host is a way out for anything the program can read — files shared into the run, and whatever a host tool returned. Entries are validated rather than trusted: a bare `str` raises `TypeError`, since `egress_allow="pypi.org"` type-checks and becomes seven single-character hosts with no refusal anywhere; blanks, whitespace and commas raise `ValueError`, because each is a way for the spec, the description the model reads, and a backend's allowlist to disagree silently. Duplicates are dropped rather than refused: two callers naming the same host is agreement. And the description the model reads changes with the allowlist — it stops claiming the sandbox has no network and names the hosts, because a model that cannot tell what it may reach spends calls finding out, and a program can enumerate the allowlist by trying it in any case.

Whichever mode is derived, the router serves it **only on a backend that enforces that exact mode** and refuses otherwise, with no substitution in either direction: an `ALLOWLIST` run on a backend that can only close is a refusal at attach, not a quietly offline program. Enforcement is [`../network.md`](../network.md).

## A fresh run per call

`acquire` is get-or-create, so the same sandbox serves every call in a conversation. The per-call directory is what keeps that from being a correctness bug: without it a file deleted from the store between rounds is still there for the next program to read as current, and last round's output is collected as this round's — a stale answer presented as a live one, in a kind whose job is transforming files.

The path comes from `session.guest_call_path()`; the framework owns it and reclaims it in a `finally` when the call returns ([`../tool-call.md`](../tool-call.md)). The run id is chosen **before** `acquire`, so a declared output name can be judged against the guest path it will actually become — the prefix is 13 bytes of the 255 a name gets.

The layout inside it depends on whether a registry is wired, and the kind derives everything a model can name from one prefix so the three uses cannot disagree about a run's shape:

| | no registry | with a registry |
|---|---|---|
| Program | `<call>/program.py` | `<call>/host_tools/program.py` |
| The model's files (`files=`, `outputs=`, the manifest) | `<call>/` | `<call>/work/` |
| Reserved names | `program.py`, plus `outputs.json` in `MANIFEST` mode | `outputs.json` only |

The two-directory split is why the transport's names are not reserved against a model-supplied one: there is nothing for the two to collide over. `program.py` stays reserved in the flat case, and each reservation carries its own refusal clause, because the two are reserved for opposite reasons — this tool *writes* the program and only *reads* the manifest.

Caps are checked before the read they would have prevented, not after: the file count before the listing, the program's own bytes before the store is touched, each shared file's as it arrives. A bound that answers only once everything is in memory has already spent what it exists to bound.

## The empty registry is the security story

Nothing is callable by default. With no registry there is no `HOST_TOOLS` in `requires`, no shim beside the program, no guest module to import — **the middleware-bypass channel does not exist until a host registers something.** That matters more here than the usual off-by-default, because calling a host tool is the one direction in which trust crosses *outward*: a registered function's body runs in the host process with the host's authority, driven by model-written code, and the middleware chain sees only `execute_code`'s aggregate result.

So the moment a host registers anything, the kind reclassifies itself rather than staying quiet. Any `Identity.USER` tool raises the whole surface to `always_require` approval, since which host-tool call would exercise the caller's own authority is not knowable before the program runs. A tool that declares a sink — **or leaves the question unanswered** — makes the host's `outbound_max_confidentiality` apply, because an unstamped tool is read as carrying something out like every other undeclared leg. That last cap is written by hand rather than derived, since a registry can carry something out with no landing artifact to say so, which is the one flow a derivation reading only the spec cannot see. Registration, identities and denial are [`../hosts.md`](../hosts.md).

The source is never a command line on either road. With no registry the command is a fixed two-element argv, `["python3", "<call>/program.py"]` — a sequence, so no shell runs at all. With one, `host_tool_calls_over_exec` does use `sh`, but every path in the line is fixed or generated host-side and single-quoted by `maf_sandbox`; the model contributes none of them.

## Withholding guest output, so the result can be classified

`execute_code`'s result mixes two kinds of content: facts the host observed — the exit code, which declared outputs landed and where — and free text the guest authored, its `stdout` and `stderr`. A host classifying tool results for information-flow control has to label that one string, and neither label is right. **Trusted** stamps whatever a program read out of a `files=` input and printed, which is the normal path rather than a contrived one, since `files=` exists precisely to hand the program data the host supplied and that data is not always the host's own. **Untrusted** taints the conversation, and in the hosts where this matters a tainted context gates the privileged sinks — so the first call costs the agent its ability to write anything afterwards, which is the end of the author → run → fix loop CodeAct exists to make cheap.

The two are separate values right up to the join: `ExecResult` carries `stdout`, `stderr` and `exit_code` as distinct fields and the landed artifacts are a distinct sequence, and they become indistinguishable only where the returned string is assembled. So the fix belongs here rather than above the kind — a host separating them again has to match the rendered `stdout:` / `stderr:` section headers, which is parsing a format this package owns and is free to change. That fails *open*, and quietly.

`make_codeact_tools(withhold_guest_output=True)` keeps the guest's text out and answers with sizes and the host's own references instead: the exit code on every run, the size of each stream, whatever `_format_landed` says about the declared outputs, and one constant sentence naming the route. No guest-authored text survives into the result, so the tool declares `source_integrity="trusted"` — the declaration is the feature, not a side effect of it, because withholding the text and still declaring nothing would leave the framework's untrusted default in place and the conversation tainted exactly as before. Content still reaches the model by the other road: written to a declared output, landed in the sink, and read back through whatever already-classified file-reading path the host has.

**What "trusted" claims, and what it does not.** The prose and the shape are this package's, and the artifact names are the model's own — but three values are the program's to choose: the exit code and the stream sizes. A program can signal through them: an exit status is 8 bits, and padding output to a chosen length carries a few more. The claim is that no guest-authored *text* crosses, not that no guest-influenced *bit* does. That is a narrow per-call channel against the open one a rendered `stdout` is, and a host that must close it should not attach this workload.

**The sink's `display` is not rendered in this mode.** `OutputSink.deliver` receives an `Artifact` whose `content` is the guest's bytes, and nothing in the protocol requires `display` to be independent of it — the docstring says only that `display` is what the model sees, against `handle`, which nothing renders. A sink composing `display` from content would therefore put guest-authored text inside a result this kind had declared trusted, and the kind cannot check that it did not. So withheld, a saved file is named by the spelling the model declared: it gives up the sink's own detail, and it needs no promise from the host to stay honest. Shown, `display` is still the better string and is still what is rendered.

**A wired registry keeps its say.** `HostToolAggregate.result_integrity` is the weakest tier over every registered source, and an unstamped tool has already failed safe into it; where it reads `untrusted` this tool declares nothing, withheld or not. Withholding governs this kind's own rendering, and where a host tool's data came from is core's fold rather than something this option may overrule.

**The replacement text is not silence.** With the streams withheld and nothing said, a failing program's entire result is its exit code, which a model cannot act on. The sizes and the route sentence are what fill that, and the model is told in the *description* as well, so it writes to a declared output on the first call instead of discovering the withholding from a result it cannot use.

**`CodeactOutputs.DECLARED` is required**, and the other two modes are refused at construction, each for its own reason. `NONE` leaves nowhere for content to go, so no call could return anything usable — the same rule every other impossible pairing in this factory follows. `MANIFEST` is the subtler one: the *program* names its own files in `outputs.json`, and this kind renders those names back into the result, so a guest-chosen string would sit inside a result declaring it holds none. Under `DECLARED` the model names the files, so the names echoed back are its own.

**A failed program still has its files collected**, which the shown path deliberately skips — there, a missing-file report stacked on a traceback buries what the model has to fix. Withheld there is no traceback to bury, and the declared output is the only channel left, including for a program that caught its own error and wrote the diagnosis into one.

Two paths need their own answer, and both are places this could have failed open unnoticed.

`SandboxProgramTimeout` embeds the program's output *in its message* — not only in `output` — so surfacing the message whole would carry guest text out under a result claiming trusted integrity. Withheld, the sentence is rebuilt from the exception's attributes instead, which is what `signal` is documented for: `"absent"` is its one value asserting a program was never started, so that distinction survives, and every other value is a degree of not knowing and is claimed neither way. Off the host-tool transport the sentence names neither a *run* nor a bound: `SandboxProgramTimeout` is public and a backend may raise one from a call of its own, whose timeout is not the number handed to `exec`.

**On the host-tool-call transport, `stderr` is the host's field, not the guest's** — the launcher merges the program's own stderr into its output file, so nothing else ever writes there. What it carries is the host's note about the run, including the one that tells a dropped output apart from a program that printed nothing. Reducing that to a byte count would report the first as the second, which is exactly the false-success report `_why_no_output` exists to prevent, so on that transport the note is surfaced whole and only the merged stream is sized. The model is told the same: one `output` count rather than two, and a `note` line that is the host's.

## Degrade paths

The kind follows the glue's ladder without softening it ([`../architecture.md`](../architecture.md) § *The MAF glue*). No router, or a router with no backend: `[]` comes back and no tool is attached, so the agent keeps the ungrounded behaviour it already had. A backend that cannot serve the assembled spec: **raise**, at construction. Everything after that is a returned string rather than an exception, because a MAF tool answers with `str` and a refusal the model never sees ends the turn mute — and each string is chosen for whose failure it is. A `SandboxProgramTimeout` is surfaced whole, because only the transport's own message knows *which* of its bounds expired and what was attempted on the program — except under `withhold_guest_output`, where that message carries the program's own output and the sentence is rebuilt from `signal` instead (above); a bare `TimeoutError` with no host-tool call is this call's one bound and says so; the same error *with* a host-tool call is a backend control-plane call and is reported as "could not run the program", because blaming the program would be a guess about code the model is about to rewrite. Provider text never reaches the transcript.

## The `RUN_CODE` road it does not take

This kind hard-requires `EXEC`, and the source reaches the interpreter as file content: `program.py` is written into the run's directory and executed as a fixed two-element argv, `["python3", "<call>/program.py"]`. `RUN_CODE` — evaluate code in a language runtime without going through a shell — is the CodeAct verb, and it is exactly what an embedded-interpreter backend would offer instead.

**The method now exists.** `Sandbox.run_code(code, *, timeout)` is a protocol member, with wall-clock timeout semantics from the call rather than from the moment the program starts, and `SandboxQueuedTimeout` to tell *never started* apart from *overran*. What the runtime promises a program — whether the last expression's value comes back or only what it printed, and what is importable — is each backend's to state, because the protocol cannot make it uniform, so a kind that assumed one shape would work on one backend by accident.

**Nobody serves it.** Every shipped backend implements the method and every real one refuses: acas, docker and wslc each raise `NotImplementedError`, because *which* runtime an image carries is a property of the image and none of them parses the reference it is handed. Only `maf_sandbox.testing`'s in-process fake answers with a result, and it scripts rather than evaluates. So the capability is declared by no backend today, and the cost of this kind's `EXEC` requirement is still zero.

The moment a backend does declare it, this kind cannot run there: the capability match is a subset test, and `{EXEC, FILES_IN}` is not satisfied by a backend offering `{RUN_CODE, FILES_IN}` however well it could actually serve the workload — the same program text would run either way, written to a file and exec'd or handed to `run_code` whole.

There are two ways out and the choice is still not made. A **matcher disjunction** — an additive `requires_any_of` the router requires each group to intersect — is honest about the workload but puts an or-expression into a vocabulary whose whole value is that it is a flat set matched by subset, and it obliges the kind to learn which member it was served under, which nothing sanctions today. A **second spec** keeps the match trivial and asks the host to attach the variant matching its backend, which is where backend knowledge already lives, at the cost of one workload carrying two spec identities. Either way the attach gate stays the only feature detection, and `DEFAULT_CAPABILITIES` keeps `EXEC`, so a say-nothing spec still refuses a `RUN_CODE` backend. [#425](https://github.com/sokolaidev/maf-extensions/issues/425) is where it gets decided, and until it does, an embedded-interpreter backend and this kind cannot meet.

## Status

| Decision | State | Tracking |
|---|---|---|
| `execute_code` over `EXEC`; the source reaches the interpreter as file content, never as a command line | shipped | — |
| Withholding guest `stdout`/`stderr` so the result is host-generated and can be declared `"trusted"`, with the mode restricted to `DECLARED` and the timeout message rebuilt from `signal` | shipped | [#769](https://github.com/sokolaidev/maf-extensions/issues/769) (closed) |
| `requires` assembled from the wired channels; `FILES_OUT` for the host-tool-call transport as well as for outputs | shipped | — |
| The file-store channel — files in, and the two call-time naming roads | shipped | [#132](https://github.com/sokolaidev/maf-extensions/issues/132) (closed) |
| Output collection via `DECLARED` and `MANIFEST`, host-supplied sink, never `FILES_LIST` | shipped | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) (open, the umbrella) |
| Host-tool calling wired, with the round trip measured on a live backend | shipped | [#133](https://github.com/sokolaidev/maf-extensions/issues/133) (open umbrella, parts A–C landed); cost measured in [#302](https://github.com/sokolaidev/maf-extensions/issues/302) (closed) |
| A fresh call directory per call, reclaimed by the framework | shipped | [#496](https://github.com/sokolaidev/maf-extensions/pull/496) (merged), kinds wired in [#500](https://github.com/sokolaidev/maf-extensions/pull/500) (merged) |
| Two-halved `egress_allow`, closed by default, validated entries | shipped | [#403](https://github.com/sokolaidev/maf-extensions/issues/403) (open) — the empty half still cannot say whether it means "needs none" or "nobody asked" |
| The mode is derived from the union rather than passed: hosts run `ALLOWLIST`, none runs `CLOSED`, `UNRESTRICTED` is not expressible | shipped | [#525](https://github.com/sokolaidev/maf-extensions/issues/525) (open, the per-kind record) delivered in [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| A `RUN_CODE`-only backend serving this kind: matcher disjunction or a second spec | open — undecided, and costing nothing yet because no backend declares `RUN_CODE` | [#425](https://github.com/sokolaidev/maf-extensions/issues/425) (open); the method itself shipped, [#381](https://github.com/sokolaidev/maf-extensions/issues/381) (closed) |
| The kind hand-builds its declarations, so a key `sandbox_tool_declarations` learns later skips it | shipped — the kind hand-builds nothing now. It passes `also_carries_out`, the one fact only the kind can see (a registry carrying something out with no landing artifact to say so, which neither `egress_allow` nor an output sink reveals), and the derivation folds it into its single rule — so a key that rule learns later reaches this tool too | [#366](https://github.com/sokolaidev/maf-extensions/issues/366) (closed) by [#581](https://github.com/sokolaidev/maf-extensions/pull/581) and [#604](https://github.com/sokolaidev/maf-extensions/pull/604) (both merged) |
| Acting on a kill or a reclaim that did not land | shipped — the reclaim is the framework's, the timeout message says what the stop reached ([#511](https://github.com/sokolaidev/maf-extensions/pull/511) (merged)), and a stop that reached less than the process group makes the framework dispose the sandbox ([`../tool-call.md`](../tool-call.md) § Cleanup) — nothing for this kind to do, and nothing it can lower | [#435](https://github.com/sokolaidev/maf-extensions/issues/435), [#617](https://github.com/sokolaidev/maf-extensions/issues/617) |
| A guest-OS axis — this kind execs `python3`, and host-tool calling additionally needs `sh` and `nohup` | shipped in core, unused here — `requires_os_family` exists and this spec leaves it `None`; the three commands are questions about the image rather than about the guest's shape, which the axis deliberately cannot answer ([`../guest-platform-and-commands.md`](../guest-platform-and-commands.md)) | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged) |
