# Shell script validation

`maf-sandbox-shell` validates Bash, POSIX sh, dash and PowerShell source in disposable sandboxes. The host enables `bash_validate`, `pwsh_validate`, or both. The model supplies only an explicit list of store-relative files. Validation runs no deployment commands, downloads no dependencies and writes no files to the host store.

## Host policy

The host selects the image, flavor, rule exclusions, severity threshold, default shell dialect and execution limits at attachment. The default threshold includes every severity. Inline suppressions are honored and individually reported, including unused declarations; suppression declarations and suppressed diagnostic counts are distinct measurements.

A recognized shebang selects Bash, sh or dash from a fixed mapping. Bash uses `bash -n`; sh and dash use `dash -n`, with ShellCheck's corresponding dialect. The launcher never executes a shebang or forwards its arguments. Without a shebang, `.bash` and `.dash` select their named dialect; `.sh` uses the host's default, initially Bash. Extensionless files require a supported shebang. Conflicting dialect declarations are refused.

PowerShell dependencies are supplied by the host image. Approved module and managed-assembly identities belong to the host's dependency inventory. The launcher verifies that inventory, then the fixed PowerShell helper preloads its managed assemblies and permits command discovery from its module directory. Submitted `.psm1` files are analysis inputs, not importable dependencies: source travels through a data bundle that the launcher removes before analysis, and analyzers receive source through anonymous pipes. The image contains PSScriptAnalyzer and its fixed rule set; no custom rule path or analyzer settings can come from the manifest. See the [image preparation guide](../../../images/shell-sandbox/README.md).

## Results

Validation completes only when every required syntax, lint and suppression-audit phase completes. A completed result is `clean` when no unsuppressed diagnostic reaches the host's threshold, or `findings` otherwise. Suppressions alone do not prevent `clean`. Missing dependencies that prevent analysis, syntax errors that stop linting, exhausted limits and malformed reports produce `completed=False` with no verdict.

Trusted summaries select package-owned rule IDs, severities, fixed explanations and `files[N]` references. Raw messages, file names, locations, counts and suppression justifications remain workload output. A fixed readable indication accompanies reports containing suppressions. Hidden argument names withhold detailed reports. A clean result describes the configured static checks, not deployment success or runtime compatibility.

## Isolation and qualification

Both flavors require a POSIX sandbox with at least container isolation, closed egress, `EXEC`, `FILES_IN`, call-scoped ownership and disposal. Analyzer phases share bounded time and output allowances; the backend deadline also bounds preparation. The manifest admits at most 64 files, each within the host's transfer limits; the encoded source bundle must fit a single transfer and 16 MiB. The report cap is 1 MiB. The host image and its release evidence establish the launcher source; an envelope's version fields establish compatibility, not authenticity.

PowerShell qualification covers analysis-time module resolution, host assembly loading, command discovery, dynamic parameters and submitted dependency shadowing. ShellCheck qualification covers parser-recognized suppressions, mixed dialects, manifest-only source reads and configuration refusal. Docker and ACAS checks establish runtime behavior separately from offline unit tests. The [local qualification record](../research/shell-validation-qualification.md) describes what was observed and what remains unverified.

## Status

| Decision | State | Tracking |
|---|---|---|
| Host-selected shell validation and suppression reporting | Implemented; Docker qualified | [#2063](https://github.com/sokolaidev/maf-extensions/issues/2063) (open); implementation by [#2097](https://github.com/sokolaidev/maf-extensions/pull/2097) (merged) |
| PowerShell host-provided dependencies and runtime qualification | Docker qualified; ACAS pending | [#2063](https://github.com/sokolaidev/maf-extensions/issues/2063) (open); implementation by [#2097](https://github.com/sokolaidev/maf-extensions/pull/2097) (merged) |
| Published shell image and consumer evidence | Local analyzer fixtures and public SDK checks passed; 55 High scan matches block publication after dependency updates; ACAS pending | [#2164](https://github.com/sokolaidev/maf-extensions/issues/2164) (open) |
