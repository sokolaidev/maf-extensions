# Third-party notices

This package's dependencies carry their own licences in their own distributions, and this file does not restate them. What it records is the thing a dependency list does not show: a place where `maf-sandbox` **reimplements logic** from another project instead of calling it, and which therefore has to track that project.

## Microsoft Agent Framework — `agent-framework-core`

Source: <https://github.com/microsoft/agent-framework> — MIT licence, Copyright (c) Microsoft Corporation.

`maf_sandbox.maf._reduced_form` reimplements the reduction `agent_framework.security` performs when it substitutes a stored payload into a tool's arguments: a mapping, or JSON text naming a `response`, reaches the tool as that field alone rather than whole — and `str()` of it where the reference was spliced into surrounding text, so a field that is not text still arrives as a name. **No code is copied** — the function is written here, and the two share a rule rather than an implementation — but the behaviour is deliberately the same, because the check it serves compares an argument against what that argument could actually have arrived as. Mirroring it wrongly is not a style problem: a payload shape the mirror misses is one that carries hidden content past the check.

That rule lives inside the framework rather than in anything it publishes, so nothing upstream promises to keep it stable. `TestValuesHoldingHiddenContent` in this package's suite asserts what the framework *delivers* for each payload shape before asserting what this package reports, so a change there fails that test rather than silently reopening the channel.

Recorded because reuse of logic survives no dependency metadata and is easy to lose in a refactor. It is not a claim that either project owes the other anything: both are MIT.
