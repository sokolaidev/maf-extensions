# Changelog

All notable changes to `maf-sandbox-bicep` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox-bicep` was split out of `maf-sandbox-aca` as the first sandbox *kind*: `bicep_validate`, a Microsoft Agent Framework tool that writes an agent's authored files into a sandbox, runs `bicep build` and `bicep lint` there, and returns the compiler's SARIF diagnostics as structured text. It imports no Azure SDK and no sandbox lifecycle code — only `maf-sandbox`'s protocol and `agent-framework-core`. This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the tool itself.

The sandbox specification it builds allows exactly the hosts a Bicep restore reaches: the two MCR artifact hosts, plus the two the **public module index** is served from — `aka.ms`, which the CLI hard-codes, and `live-data.bicep.azure.com`, where that redirects. The index fetch belongs to restore rather than to analysis, so it happens on every `bicep build` and `bicep lint`; blocked, `use-recent-module-versions` reports "Could not download available module versions" once per file instead of finding the outdated `br/public:avm/...` pins it exists to find.
