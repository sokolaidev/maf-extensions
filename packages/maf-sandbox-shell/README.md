# maf-sandbox-shell

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-shell)](https://pypi.org/project/maf-sandbox-shell/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-shell)](https://pypi.org/project/maf-sandbox-shell/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-shell/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxShellExperimentalWarning`.

Validate Bash, POSIX sh, dash and PowerShell scripts with fixed offline analyzers in disposable sandboxes. The host selects the flavor, image and policy; the model supplies an explicit file manifest. Inline lint suppressions are honored and reported.

This package is experimental and has not been released. See the [shell kind guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/shell.md) for the accepted design and qualification status.

```python
from maf_sandbox_shell import ShellFlavor, make_shell_tools

tools = make_shell_tools(
    router, file_store, agent_id, context,
    flavor=ShellFlavor.BASH,
    image="your-qualified-shell-image",
)
```

The Bash flavor accepts `.sh`, `.bash`, `.dash` and extensionless files with a supported shebang. PowerShell accepts `.ps1` and `.psm1`; dependencies must be installed in the host image's approved inventory. Both tools require closed egress, POSIX, `EXEC`, `FILES_IN`, call isolation and disposal. Validation never writes the host file store or deploys infrastructure.

Completed checks return `clean` or `findings`. Every unsuppressed severity counts by default; the host can set a threshold and excluded rules through `ShellPolicy`. Unfinished analysis returns no verdict. Exact diagnostics and suppression details remain untrusted workload output; trusted summaries use package-owned text and `files[N]` references.
