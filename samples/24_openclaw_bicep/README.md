# OpenClaw Bicep validation from published packages

Run an isolated OpenClaw Gateway with the published Bicep MCP worker and a Docker compiler. The sample submits invalid Bicep, validates a repair, and checks both actual tool results and cleanup. No Azure resources are deployed. You do not need an existing OpenClaw installation or a locally built worker wheel.

## Quick start

Use a checkout of this repository with `uv`, Node/npm and Docker on `PATH`. Docker must run Linux/amd64 containers. The qualified Node version is 26.8.1; OpenClaw requires Node `>=24.16.0 <25` or `>=26.1.0`. Setup downloads the Python worker, its dependencies, OpenClaw and the compiler image, so it needs internet access and can take several minutes.

From the repository root:

```bash
uv run --no-project samples/24_openclaw_bicep/agent.py
```

The default smoke mode uses a deterministic local provider and needs no model account. The launcher prints the private output directory. To choose its location, add `--output <new-private-directory>` outside the checkout. Every attempt requires a new directory; protect it with your user's filesystem permissions. It contains credentials, Gateway state, logs and raw responses and must not be shared or committed. The temporary wheel download is removed when execution ends; private run state remains for diagnosis.

The host verifies the published worker SHA-256 before installing it. The worker runs in its own environment, with core 0.53.1, Bicep 0.25.1, Docker backend 0.30.6, MAF 1.24.0 and MCP 2.2.0. Worker 0.3.1, OpenClaw 2026.9.7 and the compiler image digest remain the qualified candidate from [#2545](https://github.com/sokolaidev/maf-extensions/pull/2545). Other transitive versions resolve at installation and are recorded; this is not a complete dependency lock. The launcher itself uses only the Python standard library and its canonical sample scaffold, so its PEP 723 dependency list is empty.

The launcher calls the [existing Gateway harness](../experimental/openclaw_bicep_e2e/agent.py) in the same process, preserving its signal handling and resource ownership. Keep the checkout intact; downloading only this numbered directory is insufficient. The harness installs the pinned OpenClaw package into the private output directory and uses fresh configuration, sessions and worker ownership state. No existing Gateway configuration is changed.

## What success proves

Successful execution prints measured `invalid` and `valid` compiler results and a final `passed: true`. Acceptance requires the exact submitted source, expected configuration and image, confirmed per-call cleanup, stopped service processes, a released owner lock and zero owned containers. Any failed observation or incomplete cleanup makes the command fail. `report.json` in the private directory records the installed distribution versions and worker module hashes, and `setup.log`, `worker.log` and `gateway.log` retain private diagnostics. Assistant prose cannot satisfy these checks.

## Optional real model

Put these variables in a private env-file outside the repository:

```dotenv
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_MODEL=your-tool-capable-model
OPENAI_API_KEY=your-local-credential
```

Then run:

```bash
uv run --no-project --env-file /path/to/private/model.env samples/24_openclaw_bicep/agent.py --mode model
```

The endpoint must support streaming Chat Completions with function tools and `reasoning_effort: none`. Model mode incurs your provider's inference charges. It uses the same result and cleanup requirements and disables reasoning, matching the recorded compatibility profile. Other models and providers need their own successful run.

## Hosted verification and limits

The `openclaw-bicep` job in [Docker live](../../.github/workflows/docker-live.yml) runs this deterministic published-package sample on pushes to `main`, the daily schedule and ordinary manual dispatches. To run only this profile on a branch:

```bash
gh workflow run docker-live.yml --ref <branch> -f openclaw_bicep=true -f openclaw_mode=smoke
```

The job requires no model credentials. Its collector independently checks the owner scope and retains only bounded Markdown evidence, including launcher/harness hashes, installed package identities and cleanup observations. Routine smoke does not execute a real model or the fifteen-case recovery campaign; those remain separate explicit profiles.

The [qualification record](../../docs/sandbox/research/openclaw-gateway-qualification.md) retains candidate-specific evidence. A passing sample does not establish production readiness, unattended recovery, host/daemon-loss behavior, arbitrary process-tree cleanup, credential delegation or additional workload tools. The compiler remains closed-network and credential-free.
