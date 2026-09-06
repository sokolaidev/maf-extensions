# maf-sandbox-bicep

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxBicepExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_bicep.MafSandboxBicepExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework).

Sandboxed Bicep validation as a Microsoft Agent Framework tool: `bicep_validate` writes the files an agent authored into a sandbox, runs `bicep build` and `bicep lint` there, and returns the compiler's SARIF diagnostics as structured text — T2 (compiler truth) instead of T0 (the model checking its own work).

```
app  ->  maf_sandbox  ->  a backend (maf-sandbox-acas, ...)  ->  this workload
```

This package is a sandbox **kind** in the sense of [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s protocol. It contains **no Azure import and no sandbox lifecycle code**; it asks a `SandboxRouter` for a sandbox and gets back `write_file` and `exec`, so the same tool runs unchanged against ACA Sandboxes, a local Docker container or an in-process fake. Tests enforce both boundaries: one scans this package's sources for any Azure import, the other for any import outside what its manifest declares.

## Quickstart

```bash
pip install maf-sandbox-bicep
```

```python
from maf_sandbox_bicep import make_bicep_tools

tools = make_bicep_tools(router, file_store, "devops-engineer", context,
                         image="bicep-sandbox:0.46.1")
```

Pass `router=None` — or a router with no backend — and you get `[]` back: an unconfigured host attaches no tool rather than one that fails when called.

`router`, `file_store` and `context` are the host's, and this snippet shows none of them being built. [`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) is the whole wiring as a runnable program: a one-turn agent that validates a deliberately flawed Bicep file and prints the compiler's diagnostics.

## Threat model

**Fixed command templates.** No agent-authored text is interpolated into a shell command — the only substitution is a filesystem path, and only after that path is validated against the caller's file store listing (the injection guard: a name that isn't in the listing, or that resolves outside the work dir, is rejected before it reaches a template). **Sanitized error surfaces.** Failures the sandbox reports are cleaned before the model sees them, so a compiler or shell error cannot smuggle sandbox-internal detail back into the conversation. **The egress allowlist.** The only hosts Bicep is allowed to reach are the four an AVM module restore reads from — `mcr.microsoft.com` and `*.data.mcr.microsoft.com` for the artifacts, `aka.ms` and `live-data.bicep.azure.com` for the public module index — stated as a property of the spec (`SandboxSpec.egress_allow`), not of runtime configuration, because a deployment that could widen Bicep's egress after the fact could undo the containment the tool's design rests on. Nothing else is reachable, ARM above all.

## What is Bicep-specific

What is Bicep-specific — the command templates, the accepted extensions, the SARIF parsing, the hosts Bicep is allowed to reach (the four an AVM module restore reads from) — lives here and only here. The spec pins the egress allowlist and work directory as properties of the workload, not of configuration: a deployment that could widen Bicep's egress could undo the containment the tool's design rests on.

Its companion artefacts live outside this package, because a container image and a registry are not Python: a pinned Bicep image on Azure Linux, and the registry and pull identity that serve it. The hard-won behaviours of the pinned CLI — SARIF on stderr for `build` but stdout for `lint`, `build-params` for `.bicepparam`, config discovery only by walking up from the source file — are documented where they bite, in [`_tool.py`](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-bicep/src/maf_sandbox_bicep/_tool.py).

## Upgrading to 0.14

`0.14.0` requires `maf-sandbox` 0.34, which is where the sentence below can be committed at attach.

**`bicep_validate` answers with a list of content items rather than one string.** The first item is what it always returned — the diagnostics, or the sentence saying why there are none. The second is a standing sentence about the tool, labelled `trusted`, so a host whose information-flow middleware hides an untrusted result hides the diagnostics and leaves that sentence readable. **A host that only lets the model read the result needs no change**, and neither does one reading the framework's function-result content: `.result` there is the items' text joined with newlines — measured on `agent-framework-core` 1.13.0 and 1.17.0, the floor this package declares and the newest published — so the sentence simply arrives as a trailing line, with the items themselves on `.items`. What does change is a host reading the tool's answer **directly**, through `invoke(..., skip_parsing=True)` or by calling the body: that is a `list[Content]` where it was a `str`, and `str()` of a list renders item reprs rather than their text. Read `.text` off each item instead.

Why the sentence exists: an untrusted result the framework hides is replaced by a variable reference, which reads exactly like a compile that found nothing. The sentence says that a result the model cannot read is not a clean validation, and it is on every return path — refusals included — because that is what licenses labelling it `trusted` at all. The tool commits that sentence when it is attached, so `maf-sandbox` refuses a result departing from it rather than taking this package's word.

## Upgrading to 0.9

`0.9.0` requires `maf-sandbox` 0.19, which made the egress mode a thing a workload declares rather than a thing a backend is merely checked against.

**`bicep_sandbox_spec` takes an `egress` argument, and it defaults to `Egress.ALLOWLIST`.** That is the mode the kind has always wanted — the module hosts it fetches from are its reason for having an allowlist at all — but it is now *asked for* rather than implied, and the router refuses a backend that cannot enforce it:

```
SandboxEgressNotEnforced: sandbox backend 'docker' cannot enforce the 'allowlist'
egress the 'bicep' workload runs in (it enforces closed).
```

**A deployment that worked before can hit that on upgrade with nothing else changed**, because until 0.19 a `closed` backend serving an allowlist spec was permitted with a warning and simply failed at whatever it could not fetch.

Two ways out, and which is right depends on what you meant. If validation is supposed to reach the module registry, give the backend a mode that can enforce an allowlist — for `maf-sandbox-docker` that means configuring `egress_proxy_image`. If it is supposed to run offline, say so: `bicep_sandbox_spec(egress=Egress.CLOSED)` drops the host list with it, and restore failures then surface as diagnostics rather than as a refusal at attach.

`Egress.UNRESTRICTED` is accepted too, and is the honest choice for a backend that confines nothing — a no-isolation local backend, say — rather than letting it claim a confinement it does not perform.

## Upgrading to 0.6

`0.6.0` follows `maf-sandbox` 0.11, which retired the word `workspace` from the vocabulary. It requires that release; there is no version of this package that works against both.

**`make_bicep_tools` takes `file_store` where it took `workspace_store`.** It is the second positional parameter, so a call that passes it positionally needs no edit at all — only a keyword one does.

**`safe_workspace_path` is `safe_listed_path`.** Same signature, same behaviour; it validates a name against the caller's file store listing, which is what the new name says and the old one did not.

## Provenance

Split out of `maf-sandbox-acas` (which keeps the ACAS backend and nothing else) so this workload's dependency set states its portability: `maf-sandbox` + `agent-framework-core`, nothing more. Extracted from a production agent application, where it runs against real infrastructure code an agent wrote — which is where every behaviour documented above was learned.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
