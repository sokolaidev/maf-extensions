# Security policy

## Reporting a vulnerability

Report it privately, through [a draft security advisory](https://github.com/sokolaidev/maf-extensions/security/advisories/new) on this repository (**Security** → **Report a vulnerability**). Please do not open a public issue, discussion or pull request for it.

Say which package and version, which Python version and which backend, and include the smallest reproduction you have. A way for a tool call to reach past the isolation a backend declares — out of the sandbox, past its egress policy, or to a credential the host holds — is exactly what this channel is for. So is a defect in the workflows that publish these packages to PyPI.

You will get an acknowledgement within seven days. A confirmed vulnerability is fixed in a new release of the affected package, and the advisory is published with it, crediting you unless you ask not to be named.

## Supported versions

Every package here is on its `0.x` line, and only its newest release is supported. A fix ships in the next release of each affected package, cut from `main`; earlier releases are not patched, so upgrading is the remedy.

A vulnerability in a dependency — `agent-framework-core`, Docker, Azure Container Apps, Hyperlight — belongs with that project. Report it here as well when the way this suite uses the dependency is what makes it exploitable.
