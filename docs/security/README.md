# Security

Security evidence covers specific package versions, images, platforms and configurations. A passing check does not establish the absence of malware, backdoors or unknown vulnerabilities. The packages remain experimental.

For private vulnerability reporting and supported versions, follow the [security policy](../../SECURITY.md).

## Development

Run checks from the repository root after `uv sync --locked`. Changes to image scanning and its evidence checks have focused offline coverage:

```bash
uv run pytest -q tests/test_image_security_workflow.py tests/test_workflow_supply_chain.py
uv run poe doc-paths
```

Run `uv run poe gate` before opening a pull request. These local checks do not build or scan the images. The [Image security workflow](../../.github/workflows/image-security.yml) performs those checks and retains evidence for each profile; [container image security](container-images.md) defines its coverage and failure policy.

When adding or correcting a release record, follow the [evidence maintenance rules](../security.md#maintaining-a-release-record). Preserve the original commit, artifact identity, date and outcomes, and distinguish local checks from hosted results. See [Contributing](../../CONTRIBUTING.md) for the repository workflow and [AGENTS.md](../../AGENTS.md) for agent instructions.

## Documentation map

| Page | Read it for |
|---|---|
| [Release security evidence](../security.md) | Release records, artifact verification and evidence maintenance |
| [Container image security](container-images.md) | Covered image profiles, scan policy and retained reports |
| [Sandbox policy and isolation](../sandbox/policy-isolation.md) | Backend admission and the host's isolation floor |
| [Sandbox host boundary](../sandbox/hosts.md) | Host tools, artifact sinks and file integrity |
| [Security policy](../../SECURITY.md) | Private reporting, disclosure and supported versions |
