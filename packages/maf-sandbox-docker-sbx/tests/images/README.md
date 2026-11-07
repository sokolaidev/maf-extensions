# Docker Sandboxes live templates

The shell fixture layers Debian's `mount` package onto a digest-pinned Docker shell kit and restores its uid 1000 user. `mount` runs inside the guest: installing it on the host does not satisfy the backend's workspace bind. The build verifies the required guest utilities and the absence of the reserved `/maf-sandbox` mount point. Package installation happens at build time, before guest network policy applies.

From the repository root, build and load the fixture:

```bash
docker build -t maf-sbx-e2e-shell:local -f packages/maf-sandbox-docker-sbx/tests/images/shell.Dockerfile packages/maf-sandbox-docker-sbx/tests/images
docker save -o shell.tar maf-sbx-e2e-shell:local
sbx template load shell.tar
```

The backend, host-state and CodeAct live tests select `maf-sbx-e2e-shell:local` by default. Override it with `MAF_SANDBOX_SBX_E2E_IMAGE` to qualify another prepared image. Application callers select the built image with `SandboxSpec(image="maf-sbx-e2e-shell:local", ...)`; this fixture does not change the backend's implicit image selection or publish an image.

```bash
MAF_SANDBOX_SBX_E2E=1 uv run pytest -q -rs -s packages/maf-sandbox-docker-sbx/tests/test_sbx_e2e.py tests/test_sbx_codeact_e2e.py
```

Host-state tests also require `MAF_SANDBOX_SBX_E2E_HOST=1`, Docker login credentials in `DOCKER_USERNAME` and `DOCKER_PAT`, and a running SSH agent. They temporarily change global sbx settings, login, secrets and network rules: run them only on a dedicated host. The [live workflow](../../../../.github/workflows/sbx-live.yml) prepares that host and retains `host.log`, `suite.log` and `shell-image.json` with the built image identity.

The independent `agent.Dockerfile` and `root.Dockerfile` fixtures cover Debian templates with non-root and root users. Build and load them as `maf-sbx-e2e-agent:local` and `maf-sbx-e2e-root:local`, then set `MAF_SANDBOX_SBX_E2E_IMAGES=maf-sbx-e2e-agent:local,maf-sbx-e2e-root:local` to add them to the file, exec, egress and host-tool cases. The live workflow runs all three templates.
