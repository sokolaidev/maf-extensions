# 05 — validate a Bicep file in a Docker container

Sample 01 with exactly one thing changed: the backend. Same agent, same tool, same file — a compiler's diagnostics rather than a model's opinion of its own output — but the sandbox is a plain Docker container on your own machine, and there is no Azure subscription anywhere in the picture.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 01's](../01_acas_bicep/agent.py): two import lines, one constructor, and the `min_isolation=` argument. Everything below the router is untouched. That is the protocol's central claim shown rather than asserted, which is why `main.bicep` here is a **byte-identical copy** of sample 01's rather than a variation on it — a different file would make the two runs incomparable, and comparing them is the whole exercise.

This is [sample 02](../02_wslc_bicep/) for everyone that sample leaves out. `wslc` needs Windows and WSL; this backend needs only a Docker-compatible engine, which macOS, Linux and Windows-with-WSL all have. The output is the same; the machine it runs on is wider.

Two things are genuinely weaker here, and neither is hidden.

**The boundary is a container, not a VM.** `DockerSandboxBackend` declares `Isolation.CONTAINER`, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — this sample has to opt the floor down explicitly to `min_isolation=Isolation.CONTAINER`, and the default would refuse this backend outright. A Docker Desktop or Colima VM does not lift that rung: one shared VM kernel serves every container, the same shape `wslc`'s WSL 2 utility VM has. A shared kernel is a reasonable place to run a compiler on a machine you already trust, and the wrong thing to put next to a deployment's credentials; the router draws that line for you and will not be argued out of it without saying so.

**Egress is closed, not allowlisted.** Every container runs with `--network none`. The Bicep workload names four hosts it would like — `mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms` and `live-data.bicep.azure.com` — and this backend, with no egress-proxy image configured, cannot allow four hosts while denying the rest, so it allows none. `maf_sandbox` permits that (a backend that can only run fully closed is accepted, with a warning) precisely because the workload was built to report the shortfall: any `br/public:` module reference fails to restore, and `bicep_validate` answers with a `MODULE RESTORE FAILED` banner saying that type checking of module inputs did not run and the validation is incomplete. Loudly wrong beats quietly wrong. `main.bicep` uses no modules, so nothing is restored and this run completes fully offline — which is exactly why this sample validates that file and not one built out of AVM modules.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Docker Desktop (macOS, Linux, Windows with WSL 2) or Docker Engine (Linux). `docker version` confirms the client can reach a running daemon. Colima, OrbStack, Rancher Desktop and Podman expose Docker-compatible sockets and may work through the same client, but they are not officially supported and nothing here is verified against them.
- **The sandbox image**, built from [`images/bicep-sandbox`](../../images/bicep-sandbox/) — the same image sample 01 runs in Azure, two layers on `mcr.microsoft.com/azurelinux/base/core:3.0`: the Bicep CLI, pinned, plus `bicepconfig.json` at `/acas/work` — sample-grade, deliberately; production replaces it with a hardened build you own (minimal, digest-pinned, scanned, rebuilt on your patch cadence) built the same way. Run this from the repository root:

  ```bash
  docker build -t bicep-sandbox:local images/bicep-sandbox
  ```

- **An OpenAI-compatible endpoint** — api.openai.com, or a local server that speaks the same protocol. The sample deliberately uses the chat-completions API, the one surface local servers implement well; their newer-API support is often partial in ways that surface as an empty final answer. The model needs to be able to call a tool; beyond that this sample asks nothing unusual of it.

No Azure subscription, no preview enrolment, no billable sandbox. A run that is killed mid-turn leaves the container **running** — it was started with `sleep infinity` and nothing stops it on the way out — so plain `docker ps` shows it, it holds engine memory until it goes, and `docker rm -f <name>` reclaims it.

## Install

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-docker maf-sandbox-bicep agent-framework-openai
```

`maf-sandbox` arrives as a dependency, and nothing Azure does — the backend drives the `docker` client and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `BICEP_SANDBOX_IMAGE` | Local image reference, e.g. `bicep-sandbox:local`. No registry qualifies it — the backend runs what is already on this machine |
| `OPENAI_API_KEY` | Key for the endpoint below. A local server that ignores it still wants something non-empty here |
| `OPENAI_CHAT_MODEL` | Model name, e.g. `gpt-4o` — or whatever your local server calls the model it serves |
| `OPENAI_BASE_URL` | *Optional.* An OpenAI-compatible base URL, e.g. `http://localhost:11434/v1`. Unset, the client talks to OpenAI |

With any of the first three unset the program says which and exits non-zero, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

```bash
python agent.py
```

The first call pays for creating and starting the container — a fraction of a second on a warm engine, against the minutes a microVM-isolated sandbox needs; if the image is not local yet, an explicit pull happens first. The model writes its own prose around them, but the diagnostics it is reporting look like this:

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

There is not even a difference in where the config came from: sample 01, sample 02 and this one all run [the same image](../../images/bicep-sandbox/), and its `bicepconfig.json` sits at `/acas/work` — the work-dir root `maf-sandbox-bicep` fixes in its spec, and the only place Bicep will find it, because Bicep resolves that file solely by walking up from the source it is compiling.

## Troubleshooting

**The image is not found** — build it, from the repository root:

```bash
docker build -t bicep-sandbox:local images/bicep-sandbox
```

**`Cannot connect to the Docker daemon`** — the client is installed but no daemon is reachable. Start Docker Desktop (or your engine) and confirm with `docker version`, which reports both a Client and a Server section when the daemon is up. This is the most common failure by a wide margin, and the backend names it rather than letting it read as a generic sandbox error.

**`the docker backend needs an event loop that can spawn subprocesses`** — only on Windows, and only if the host installed `WindowsSelectorEventLoopPolicy`. Every call spawns the `docker` client, which asyncio's default Proactor loop on Windows supports; a host that changed that has to change it back.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `min_isolation=Isolation.MICROVM` floor, so this is the router refusing a shared-kernel boundary at its default posture. It raises at construction rather than at first call, on purpose. This is not a configuration problem to work around — it is the reason this backend is a developer-machine one, opted into explicitly.

**`MODULE RESTORE FAILED` and `BCP192` on every `br/public:` reference** — the closed egress described above, working as designed. Nothing is misconfigured, and the banner is the tool refusing to let an incomplete validation read as a clean one. If you need module restore, configure an egress-proxy image on the backend, or use sample 01's backend, which can allow those four hosts and deny everything else.

**`no-unused-params` reports as `[warning]`** — `bicepconfig.json` was not found, so every linter rule is at its built-in default and the rule set is weaker than intended. Check the image really has the file at `/acas/work/bicepconfig.json`; a build that skipped the `COPY` leaves a run that looks entirely healthy.
