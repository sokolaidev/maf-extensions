# 09 — validate a Bicep file against a backend that is not isolated

Sample 02 with the backend swapped for one this sample defines — `NoIsolationBackend` — that really runs the bicep CLI on the host. Same agent, same tool, the same `main.bicep` as samples 01/02/05 — but the sandbox is a host work directory and a real subprocess, not a container or a VM. No image, no container runtime, and no boundary either: this is the floor of the isolation ladder (`Isolation.PROCESS`, "same process as the host, no boundary at all"), the rung for tests and local fakes, here carrying a real compiler instead of a scripted one. The *sandbox* costs nothing to run — no image to pull, no container runtime, no Azure subscription for the backend; the model is a separate cost, and the default is a cloud one.

```
app  ->  maf_sandbox (router)  ->  NoIsolationBackend (this sample)  ->  bicep, on the host
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 02's](../02_wslc_bicep/agent.py): the backend is the change — it lives in [`no_isolation_backend.py`](no_isolation_backend.py). Everything below the router is the same workload — `make_bicep_tools`, the fixed `bicep build` and `bicep lint` command templates, the SARIF parser — running against a backend that has a real guest filesystem (a host temp directory) and a real compiler (the `bicep` binary on this machine). That is the point of the sample: the same bicep workload that runs in a container (samples 01/02/05) runs here unchanged, against a backend with no boundary.

This is the fourth comparable Bicep sample (01, 02, 05, 09): one compiler, one lint rule set (the repo [`bicepconfig.json`](bicepconfig.json) seeded into the work directory the way the images bake it in), a different backend underneath. The protocol's central claim — a workload written against `maf_sandbox` runs unchanged on another backend — is shown rather than asserted, at the weakest boundary that can still run it.

One accommodation a host backend makes, and it is named in the code: the bicep kind fixes a guest `work_dir` (an absolute guest path, set on the spec) and builds its `bicep build` / `bicep lint` commands under it. That path is not a real path on this host, so `NoIsolationSandbox` maps the spec's `work_dir` to a host temp directory — every guest path is rewritten under it, and `exec` substitutes it into the command. The mapping is honest because `work_dir` is known from the spec, not parsed out of an opaque argv — the narrow version of what the protocol otherwise leaves to a kind.

## A temporary misuse, called out plainly

A backend with no boundary honestly cannot confine egress, which is `Egress.UNRESTRICTED` — and the router refuses `UNRESTRICTED` for any workload today, so the backend declares `Egress.CLOSED` only to pass that gate. It does not enforce it; it cannot. That gap is not inert: the bicep compiler fetches the public module *index* on every `bicep build` and every `bicep lint` regardless of whether the source references modules, so it is exercised on every run. It is still safe to ship because the four hosts the compiler reaches are the kind's own `egress_allow` — Microsoft-operated, public, unauthenticated, with no ARM endpoint and no ambient identity on the host to reach — so a no-boundary backend cannot widen what the workload was already going to touch, and containment holds in substance by the compiler's own behavior, not by this backend. The plan is to switch back to `UNRESTRICTED` once the core allows it for workloads that don't require `Capability.NETWORK` ([#265](https://github.com/sokolaidev/maf-extensions/issues/265)); the separate question of telling "said nothing" (`UNDEFINED`) from "said I confine nothing" is [#264](https://github.com/sokolaidev/maf-extensions/issues/264). The `egress` property in `no_isolation_backend.py` carries the comment that tracks this.

## Prerequisites

- **The bicep CLI on PATH.** The container images bake the binary in; this backend shells out to whatever `bicep` resolves to on your machine, so it has to be installed. The Azure docs cover the install, or match the pin the images use ([`images/bicep-sandbox/Dockerfile`](../../images/bicep-sandbox/Dockerfile) pins `v0.46.1`). `bicep --version` is the quickest check it is there and that the host has the ICU libraries the binary needs.
- **A model endpoint.** Locally, an [Ollama](https://ollama.com/) server on its default port — `ollama serve` and a one-time `ollama signin`, because the default model is a cloud one served through the local daemon (nothing to `pull`). Even the model name is optional because the sample defaults it. In CI, an Azure OpenAI deployment reached with a federated credential, no key stored anywhere. The model needs to be able to call a tool; beyond that this sample asks nothing unusual of it.
- **A POSIX shell and a space-free temp directory.** The backend runs the bicep command templates as-is — `bicep build {path} ... 2>&1 || true` — which are the container's `/bin/sh` idiom, so it assumes a POSIX shell; the Linux CI runner's `/tmp` satisfies this. Don't point `TMPDIR` at a path containing spaces: the host root is substituted into the command as a bare token, and a space would split the argument the compiler receives.

No sandbox image, no container runtime, no Azure subscription for the *sandbox* — it is a host temp directory that lives for one turn. The model is a separate cost: the default is a cloud model served through the local Ollama daemon, and the CI path uses Azure OpenAI, so inference is whatever your endpoint charges — the backend is free, the model path is not.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

`maf-sandbox` brings the router; `maf-sandbox-bicep` brings the workload; `agent-framework-openai` brings the model client. `azure-identity` and `azure-core[aio]` are declared for the CI path — a local-only run installs them too, because the block is the single source, and never imports them.

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | *Optional.* Set it to reach Azure OpenAI in CI (with a federated credential — no key). Unset, the client talks to a local Ollama server. The two paths are mutually exclusive and this one variable is the branch |
| `AZURE_OPENAI_CHAT_MODEL` | Required **only** when `AZURE_OPENAI_ENDPOINT` is set. The Azure deployment name |
| `OPENAI_CHAT_MODEL` | *Optional, local only.* Model name. Defaults to `minimax-m3:cloud`, a cloud model — so a running Ollama server and a one-time `ollama signin` is the whole of configuration |
| `OPENAI_BASE_URL` | *Optional, local only.* An OpenAI-compatible base URL. Defaults to `http://localhost:11434/v1`, Ollama's endpoint |

Local mode needs none of them — an unset `AZURE_OPENAI_ENDPOINT`, a running `ollama serve`, and `bicep` on PATH is enough. Azure mode needs the first two and exits non-zero if `AZURE_OPENAI_CHAT_MODEL` is missing, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The model writes its own prose around the diagnostics, but what it is reporting comes from the real compiler, and looks like this:

```
  [error]   no-unused-params @ main.bicep:21 — Parameter "environmentName" is declared but never used.
  [warning] BCP035 @ main.bicep:31 — The specified "resource" declaration is missing the
            following required properties: "sku".
  [warning] use-recent-api-versions @ main.bicep:31 — '2023-01-01' is 1320 days old,
            should be no more than 730 days old, or the most recent.

Disposed 1 sandbox(es).
```

Three diagnostics — the two faults the file carries (`no-unused-params`, `BCP035`) plus the age of the deliberately old API version (`use-recent-api-versions`). The rule ids are opaque tokens the model is instructed to echo verbatim, so their presence is evidence the real compiler ran and its SARIF reached the workload, was parsed, was rendered, and came back through the agent — the whole protocol seam, on the host, with nothing rented. The `Disposed 1 sandbox(es).` line is the proof a sandbox was acquired at all, and not the model answering from reading the file.

The day count and the acceptable-version list in `use-recent-api-versions` move with no code change, so the live check ([`scripts/check_live_sample.py`](../../scripts/check_live_sample.py), shared with samples 01 and 05) matches that rule's **id** and never its message. It requires `no-unused-params` and `BCP035` by name, the `Disposed` line, and one of the two things only a discovered `bicepconfig.json` produces: `use-recent-api-versions` reported at all, or `no-unused-params` reported at `[error]` rather than its built-in `warning`. Either one settles it, because a run that found no config loses both at the same moment — and a run that found no config is otherwise indistinguishable from a healthy one ([#308](https://github.com/sokolaidev/maf-extensions/issues/308)). Both are matched as *rendered* diagnostics — the rule id with a bracketed severity beside it — so a model that merely names a rule while saying it was not reported does not satisfy them. That is why this sample ships its own copy of `bicepconfig.json` and seeds it into the work root: the host has no image to bake it into.

## Troubleshooting

**`bicep: command not found` / `bicep` not recognized** — the CLI is not on PATH. Install it (see Prerequisites). The backend shells out to `bicep`; without it, the shell prints an error where SARIF was expected, the SARIF parser rejects it, and the agent reports `Error: could not parse SARIF output` — not a clean file.

**`Couldn't find a valid ICU package`** — the bicep binary needs ICU libraries and the host does not have them. The container image installs `icu` for exactly this reason; on a minimal host, install the equivalent (e.g. `libicu` on Debian/Ubuntu, `icu` elsewhere).

**`Connection refused` at `localhost:11434`** — Ollama is not running. `ollama serve` starts it; the sample's default base URL points there. Point `OPENAI_BASE_URL` at a different server if you run another.

**Cloud model not authorized (`model not found`, or a cloud/auth error)** — the default is a cloud model served through the local daemon; run `ollama signin` once to authorize it, or set `OPENAI_CHAT_MODEL` to a local model you already have.

**`Azure OpenAI client requires either an API key or an Azure AD token provider`** — the CI path was taken (`AZURE_OPENAI_ENDPOINT` is set) but the credential resolved to nothing. An `az login` (or a federated CI credential) is what satisfies `DefaultAzureCredential`; check that the federated identity is configured for this branch's subject, the way the other Azure samples do.

**No diagnostics in the output, just the model's opinion** — the tool was not attached, which means `make_bicep_tools` returned `[]`, or the compiler never ran. The router has a backend here, so the likely cause is the `bicep` binary not being found or failing at startup; `bicep --version` from the same shell is the fastest way to tell.