# Samples

Small, self-contained programs, each showing one wiring end to end. The package READMEs explain what a piece is; these show what you actually write.

| Sample | What it wires | Needs |
|---|---|---|
| [`01_acas_bicep`](01_acas_bicep/) | A one-turn agent that validates a Bicep file: `maf-sandbox-acas` behind a `SandboxRouter`, `maf-sandbox-bicep`'s `bicep_validate` attached to a MAF agent | Azure (Container Apps Sandboxes preview + Azure OpenAI) |
| [`02_wslc_bicep`](02_wslc_bicep/) | The same agent and the same `main.bicep`, one line lower: `maf-sandbox-wslc` in place of `maf-sandbox-acas`, so the workload runs unchanged against a second backend | Windows with WSL 2.9.3+ and any OpenAI-compatible endpoint |
| [`03_acas_codeact`](03_acas_codeact/) | A one-turn agent that computes an answer instead of guessing one: `maf-sandbox-acas` behind a `SandboxRouter`, `maf-sandbox-codeact`'s `execute_code` attached to a MAF agent, at the router's default `microvm` floor | Azure (Container Apps Sandboxes preview + Azure OpenAI) |
| [`04_wslc_codeact`](04_wslc_codeact/) | The same agent and the same task, one line lower: `maf-sandbox-wslc` in place of `maf-sandbox-acas`, opted down to `min_isolation=Isolation.CONTAINER` | Windows with WSL 2.9.3+ and any OpenAI-compatible endpoint |
| [`05_docker_bicep`](05_docker_bicep/) | Sample 02 for everyone `wslc` leaves out: `maf-sandbox-docker` runs the same `bicep_validate` agent in a plain Docker container, on any machine with a Docker-compatible engine | A Docker-compatible engine and any OpenAI-compatible endpoint |
| [`06_docker_codeact`](06_docker_codeact/) | Sample 03 one line lower, and the first live-verified sample with no billable sandbox: `maf-sandbox-docker` runs `execute_code` in a Docker container, against sample 03's Azure model reached with `DefaultAzureCredential` | A Docker-compatible engine and an Azure OpenAI deployment (no key — `az login`) |
| [`07_docker_diagram`](07_docker_diagram/) | The first sample to read a file **back out**: `maf-sandbox-docker` runs a `render_diagram` kind — defined in the sample, not a package — that renders Graphviz DOT to a PNG and lands it through `FILES_OUT` | A Docker-compatible engine and any OpenAI-compatible endpoint |

## How these are meant to be read

**Numbered directories.** Reading order is a property of the set, so it is written down rather than implied. Later samples assume you have read the earlier ones and skip what they already showed.

**All three Bicep samples run the same sandbox image**, [`images/bicep-sandbox`](../images/bicep-sandbox/) — sample 01 imports it into an Azure sandbox group, sample 02 builds it on the machine you are sitting at with `wslc build`, sample 05 builds it with `docker build`. That is what makes their output comparable at all: one compiler, one lint rule set, a different backend underneath. Its README carries the build, push and import command lines.

**All three CodeAct samples run the same image too**, but nobody in this repository builds it: `mcr.microsoft.com/devcontainers/python:3.13-bookworm`, a standard Microsoft Container Registry image, used by reference. Sample 03 imports it into an Azure sandbox group as a disk image; samples 04 and 06 pull it directly, the way any `docker run` does. Each README says once why a dev-container image is bulkier than this workload strictly needs.

**The docker samples (05, 06, 07) are for everyone `wslc` leaves out.** `wslc` needs Windows and WSL; a Docker-compatible engine is on macOS, Linux and Windows-with-WSL alike, and on every GitHub Actions Linux runner. Sample 05 is the local-machine Bicep story for those hosts; sample 06 is the CodeAct one, and it is also the first sample whose live verification runs a real container on this repository's own runners — see below.

**Sample 07 is the one that reads a file back out.** Where 05 and 06 write into a container and read its stdout, 07 pulls a rendered PNG out through `FILES_OUT` and lands it in host storage — and it defines its own `render_diagram` kind inline in `agent.py` rather than installing a workload package, so it doubles as the worked example of writing a kind against the published protocol alone. Its guest is [`images/diagram-sandbox`](../images/diagram-sandbox/), a Debian base with Graphviz and nothing else, built with `docker build`.

**Every image here is sample-grade, chosen for prototyping.** The dev-container image is a convenience, and even the purpose-built Bicep image is only a pinned CLI on a stock base. A production deployment replaces them with hardened images your organization builds and owns — minimal base, digest-pinned, nothing installed the workload does not use, scanned and rebuilt on your patch cadence — supplied through the same `image`/`image_id` spec fields; nothing else in a sample's wiring changes.

**Each sample installs from PyPI**, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-acas maf-sandbox-bicep agent-framework-openai
python samples/01_acas_bicep/agent.py
```

That is deliberate. A sample that only runs inside this repository would be demonstrating something a consumer of the published wheels does not get — an import that resolved because every sibling package happened to be on the path.

**No secrets in the tree.** Configuration comes from environment variables, and authentication is whatever the sample's own stack uses — `DefaultAzureCredential`, which an `az login` session satisfies, wherever Azure is involved. Each sample's README lists the variables it reads.

**They are not tests.** Samples are not uv workspace members and are not in the root `testpaths`, so `uv run pytest` does not collect them. They *are* covered by `uv run ruff check .` and `uv run ruff format --check .`, so they cannot rot into non-idiomatic code without CI noticing.

No pull request runs the Azure samples, but sample 01 and sample 03 are not left entirely to hand-running. Gating every PR on a real subscription and a preview service would trade a great deal of flakiness for a little assurance — so instead `verify-live.yml` runs them on demand and once after each release ([#33](https://github.com/sokolaidev/maf-extensions/issues/33)), where a billable sandbox per run is proportionate and a red is about the released set rather than a contributor's diff.

**Sample 06 joins that workflow, and changes its economics.** Its sandbox is a Docker container on the Linux runner rather than a billable Azure microVM, and its model is the same Azure OpenAI deployment reached with a federated credential — so it is the one live-verified sample that creates **no billable sandbox** and needs **no stored secret**. (The model still runs in an Azure subscription and bills per inference; that cost no sample avoids.) There is no reason not to run it after every release the docker or codeact packages could affect. The docker *backend* itself is exercised even more often than that: `test_docker_e2e.py` runs a real container on **every** pull request (the Linux runners have Docker), which is a first for this family — the wslc and acas live paths never could.

Samples 02, 04, 05 and 07 are not wired into `verify-live.yml`. Sample 02 and sample 04 need Windows and a WSL that ships `wslc`, which the Linux runners do not have; samples 05 and 07 need an OpenAI-compatible endpoint and a key, and there is no such secret in CI (sample 06 is the docker CodeAct sample precisely because its Azure-federated model needs none). A break in any of the four is invisible until someone runs it by hand — though for samples 05 and 07, the docker backend under them is the same one every PR exercises live, `FILES_OUT` path included.

## Planned

- The same agent against the in-process `testing` backend, so it runs anywhere — no cloud account and no container runtime.
- A multi-turn author → validate → fix loop.
- Wiring `SandboxPurger` into a host's thread-delete path.
