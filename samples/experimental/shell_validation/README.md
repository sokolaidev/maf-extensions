# Shell validation before publication

This workspace-only sample calls both shell tools through Docker, reports findings and inline suppressions, and disposes each sandbox. It needs no model credentials. The kind is unreleased, so run from the repository root against workspace packages:

```bash
uv sync --all-packages
uv run --locked python scripts/build_wolfi_python.py --platform linux/amd64 -t maf-shell:local images/shell-sandbox
uv run python samples/experimental/shell_validation/agent.py --image maf-shell:local
```

The Bash input uses POSIX sh, disables SC2086 and still reports the undefined variable. The PowerShell input reports `PSAvoidUsingWriteHost`. Neither deployment script runs and neither store entry changes. See the [kind contract](../../../docs/sandbox/kinds/shell.md) and [image recipe](../../../images/shell-sandbox/README.md) for host policy and dependency preparation.
