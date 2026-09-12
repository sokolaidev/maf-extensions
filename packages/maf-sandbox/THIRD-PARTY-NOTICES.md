# Third-party notices

This package's dependencies carry their own licences in their own distributions, and this file does not restate them. What it records is the thing a dependency list does not show: a place where `maf-sandbox` **reimplements logic** from another project instead of calling it, and which therefore has to track that project.

## Microsoft Agent Framework — `agent-framework-core`

Source: <https://github.com/microsoft/agent-framework> — MIT licence, Copyright (c) Microsoft Corporation.

`maf_sandbox.maf._reduced_form` reimplements a reduction `agent_framework.security` performs when it substitutes a stored payload into a tool's arguments: a mapping, or JSON text naming a `response`, reaches the tool as that field alone rather than whole — and `str()` of it where the reference was spliced into surrounding text, so a field that is not text still arrives as a name. **No code is copied** — the function is written here, and the two share a rule rather than an implementation — but the behaviour is deliberately the same, because the check it serves compares an argument against what that argument could actually have arrived as. What the mirror buys is precision, not the check itself: `_substituted_forms` offers the payload whole beside the reduced one, so a shape the mirror gets wrong costs a needlessly wide comparison rather than a value carried past it.

That rule lives inside the framework rather than in anything it publishes, so nothing upstream promises to keep it stable — and it has already moved. `agent-framework-core` 1.18.0 reduces a payload only where a quarantined LLM produced it, and delivers every other one whole, which is why both forms are offered rather than whichever one a single core happens to substitute. `TestPositionsHoldingHiddenContent.test_what_the_framework_substitutes_is_a_form_this_package_offers` asserts that what the framework hands a body is one of them, so an upstream substitution neither core makes fails that test rather than passing unnoticed.

Recorded because reuse of logic survives no dependency metadata and is easy to lose in a refactor. It is not a claim that either project owes the other anything: both are MIT.
