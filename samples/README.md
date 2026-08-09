# Samples

Small, self-contained programs, each showing one wiring end to end. The package READMEs explain what a piece is; these show what you actually write.

| Sample | What it wires | Needs |
|---|---|---|
| [`01_acas_bicep`](01_acas_bicep/) | A one-turn agent that validates a Bicep file: `maf-sandbox-aca` behind a `SandboxRouter`, `maf-sandbox-bicep`'s `bicep_validate` attached to a MAF agent | Azure (Container Apps Sandboxes preview + Azure OpenAI) |
| [`02_wslc_bicep`](02_wslc_bicep/) | The same agent and the same `main.bicep`, one line lower: `maf-sandbox-wslc` in place of `maf-sandbox-aca`, so the workload runs unchanged against a second backend | Windows with WSL 2.9.3+ and any OpenAI-compatible endpoint |

## How these are meant to be read

**Numbered directories.** Reading order is a property of the set, so it is written down rather than implied. Later samples assume you have read the earlier ones and skip what they already showed.

**Each sample installs from PyPI**, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-aca maf-sandbox-bicep agent-framework-openai
python samples/01_acas_bicep/agent.py
```

That is deliberate. A sample that only runs inside this repository would be demonstrating something a consumer of the published wheels does not get — an import that resolved because every sibling package happened to be on the path.

**No secrets in the tree.** Configuration comes from environment variables, and authentication is whatever the sample's own stack uses — `DefaultAzureCredential`, which an `az login` session satisfies, wherever Azure is involved. Each sample's README lists the variables it reads.

**They are not tests.** Samples are not uv workspace members and are not in the root `testpaths`, so `uv run pytest` does not collect them. They *are* covered by `uv run ruff check .` and `uv run ruff format --check .`, so they cannot rot into non-idiomatic code without CI noticing.

No pull request runs these, but sample 01 is not left entirely to hand-running. Gating every PR on a real subscription and a preview service would trade a great deal of flakiness for a little assurance — so instead `verify-live.yml` runs sample 01 on demand and once after each release ([#33](https://github.com/sokolaidev/maf-extensions/issues/33)), where a billable sandbox per run is proportionate and a red is about the released set rather than a contributor's diff. Sample 02 needs no cloud, but it does need Windows and a WSL that ships `wslc`, which the Linux runners these workflows use do not have. The planned sample on the in-process `testing` backend needs nothing at all — that one can run on every PR, and the question is worth revisiting when it exists.

## Planned

- The same agent against the in-process `testing` backend, so it runs anywhere — no cloud account and no container runtime.
- A multi-turn author → validate → fix loop.
- Wiring `SandboxPurger` into a host's thread-delete path.
