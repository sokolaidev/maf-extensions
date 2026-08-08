# 02 — validate a Bicep file in a WSL container

Sample 01 with exactly one thing changed: the backend. Same agent, same tool, same file — a compiler's diagnostics rather than a model's opinion of its own output — but the sandbox is a container on your own machine, created in about half a second, and there is no Azure subscription anywhere in the picture.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_wslc  ->  the container
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 01's](../01_acas_bicep/agent.py): two import lines, one constructor, and the `deployed=` argument. Everything below the router is untouched. That is the protocol's central claim shown rather than asserted, which is why `main.bicep` here is a **byte-identical copy** of sample 01's rather than a variation on it — a different file would make the two runs incomparable, and comparing them is the whole exercise.

Two things are genuinely weaker here, and neither is hidden.

**The boundary is a container, not a VM.** `WslcSandboxBackend` declares `Isolation.CONTAINER`, so `SandboxRouter(..., deployed=True)` refuses it outright. A shared kernel is a reasonable place to run a compiler on a machine you already trust, and the wrong thing to put next to a deployment's credentials; the router draws that line for you and will not be argued out of it.

**Egress is closed, not allowlisted.** Every container runs with `--network none`. The Bicep workload names four hosts it would like — `mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms` and `live-data.bicep.azure.com` — and this backend cannot allow four hosts while denying the rest, so it allows none. `maf_sandbox` permits that (a backend that can only run fully closed is accepted, with a warning) precisely because the workload was built to report the shortfall: any `br/public:` module reference fails to restore, and `bicep_validate` answers with a `MODULE RESTORE FAILED` banner saying that type checking of module inputs did not run and the validation is incomplete. Loudly wrong beats quietly wrong. `main.bicep` uses no modules, so nothing is restored and this run completes fully offline — which is exactly why this sample validates that file and not one built out of AVM modules.

## Prerequisites

- **Windows with WSL 2.9.3 or later.** `wslc` is WSL's container CLI and ships with it; `wsl --version` reports the version, `wslc --version` confirms the CLI is on `PATH`. Nothing else needs installing — no Docker, no daemon, no login.
- **The sandbox image, built from this directory.** It is a two-layer image: the Bicep CLI, pinned, on `mcr.microsoft.com/azurelinux/base/core:3.0`, plus [`bicepconfig.json`](bicepconfig.json) at `/acas/work`.

  ```bash
  wslc build -t bicep-sandbox:local samples/02_wslc_bicep
  ```

- **An OpenAI-compatible endpoint** — api.openai.com, or a local server that speaks the same protocol. The model needs to be able to call a tool; beyond that this sample asks nothing unusual of it.

No Azure subscription, no preview enrolment, no billable VM. A run that is killed mid-turn leaves the container **running** — it was started with `sleep infinity` and nothing stops it on the way out — so plain `wslc container list` shows it, it holds WSL VM memory until it goes, and `wslc container remove -f <name>` reclaims it.

## Install

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-wslc maf-sandbox-bicep agent-framework-openai
```

`maf-sandbox` arrives as a dependency, and nothing Azure does — the backend drives `wslc.exe` and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `BICEP_SANDBOX_IMAGE` | Local image reference, e.g. `bicep-sandbox:local`. No registry qualifies it — `wslc` runs what is already on this machine |
| `OPENAI_API_KEY` | Key for the endpoint below. A local server that ignores it still wants something non-empty here |
| `OPENAI_CHAT_MODEL` | Model name, e.g. `gpt-4o` — or whatever your local server calls the model it serves |
| `OPENAI_BASE_URL` | *Optional.* An OpenAI-compatible base URL, e.g. `http://localhost:11434/v1`. Unset, the client talks to OpenAI |

With any of the first three unset the program says which and exits non-zero, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

```bash
python agent.py
```

The first call pays for creating and starting the container — a few hundred milliseconds, against the minutes a VM-isolated sandbox needs. The model writes its own prose around them, but the diagnostics it is reporting look like this:

```
  [error]   no-unused-params @ main.bicep:21: Parameter "environmentName" is
  declared but never used.
  [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing
  the following required properties: "sku".
  [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for
  'Microsoft.Storage/storageAccounts'. '2023-01-01' is N days old ...

Disposed 1 sandbox(es).
```

Three diagnostics, the same three sample 01 gets from a microVM in Azure. Sample 01's README reads them closely and that reading applies here unchanged; the short version is that `no-unused-params` printing as `[error]` rather than its built-in `[warning]` is the visible proof that `bicepconfig.json` was discovered, `BCP035` really is a warning in current Bicep, and the day count in the last one climbs on its own.

The only difference is where the config came from. Sample 01's image is built elsewhere; here it is [`bicepconfig.json`](bicepconfig.json) in this directory, copied by the [`Dockerfile`](Dockerfile) to `/acas/work` — the work-dir root `maf-sandbox-bicep` fixes in its spec, and the only place Bicep will find it, because Bicep resolves that file solely by walking up from the source it is compiling.

## Troubleshooting

**The image is not found** — build it, from the repository root:

```bash
wslc build -t bicep-sandbox:local samples/02_wslc_bicep
```

**`wslc` is not found** — WSL is older than 2.9.3, or is not installed. `wsl --version` reports it; `wsl --update` moves it forward. There is no separate package to install: the CLI is part of WSL.

**`SandboxBackendNotPermitted` at startup** — something passed `deployed=True` to the router. `WslcSandboxBackend` declares `Isolation.CONTAINER` and `DEPLOYED_ISOLATION` is exactly `{VM}`, so this is the router refusing a shared-kernel boundary in a deployed environment. It raises at construction rather than at first call, on purpose. This is not a configuration problem to work around — it is the reason this backend is a developer-machine one.

**`MODULE RESTORE FAILED` and `BCP192` on every `br/public:` reference** — the closed egress described above, working as designed. Nothing is misconfigured, and the banner is the tool refusing to let an incomplete validation read as a clean one. If you need module restore, sample 01's backend is the one that can allow those four hosts and deny everything else.

**`no-unused-params` reports as `[warning]`** — `bicepconfig.json` was not found, so every linter rule is at its built-in default and the rule set is weaker than intended. Check the image really has the file at `/acas/work/bicepconfig.json`; a build that skipped the `COPY` leaves a run that looks entirely healthy.
