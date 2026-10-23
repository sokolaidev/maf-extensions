# Shell validation qualification

> Local observations through 2026-10-24 for [#2063](https://github.com/sokolaidev/maf-extensions/issues/2063). The accepted interface is in the [kind contract](../kinds/shell.md); reproducible image commands and dependency preparation are in the [image guide](../../../images/shell-sandbox/README.md).

## Analysis-time execution

The baseline probe used PowerShell 7.6.7 and PSScriptAnalyzer 1.25.0 in a closed-network, read-only Linux container with writable temporary storage. A supplied `.psm1` contained a file-writing sentinel. Parsing its `using module` reference did not write the sentinel. PSScriptAnalyzer command discovery for a module-qualified command did execute the module body. This proves that staging authored modules beside scripts would allow analysis-time execution in this configuration. It does not prove that every parser or analyzer construct is inert.

The implementation keeps authored files off the analyzer filesystem. The host uploads one data bundle; the fixed launcher reads and removes it before starting any analyzer. Bash, dash, the ShellCheck adapter and PowerShell receive source through pipes. The regression fixture supplies the same module body and a module-qualified command and observes no sentinel. Additional fixtures cover module/assembly references, dynamic parameters and `ValidateScript` attributes. These observations support the stated boundary for the tested versions; they are not a general proof against analyzer vulnerabilities.

Host dependencies have a separate path. A derived fixture image contains an inventoried module and managed assembly. Command discovery loads the host module, with its sentinel observed, while an authored module bearing the same name remains inert. A separate probe showed that accepting a `using assembly` statement did not itself load the assembly. The fixed helper therefore preloads the host-inventoried managed assemblies before parsing and checks their loaded locations; the fixture verifies the loaded assembly receipt. This executes host-approved dependency code inside the sandbox.

## Local checks

| Evidence | Observation |
|---|---|
| Source-pinned ShellCheck 0.11.0 adapter | Reports effective and unused disable clauses, their scopes and exact upstream rule ranges; a second parser-backed pass identifies suppressed findings |
| Bash/sh/dash fixtures | Correct grammar selection, caller-dialect sourced diagnostics and suppressions, direct-then-declared source resolution with leading `SCRIPTDIR` expansion, manifest-only source reads, heredoc text excluded from suppression declarations, syntax failures incomplete, script commands inert |
| PowerShell fixtures | Effective and unused suppression attributes reported; parse errors and analyzer failures cannot produce a clean verdict |
| Launcher supervision | Shared output budget enforced across processes; a child retaining output pipes is terminated at the deadline |
| Public tools through Docker | Both flavors return findings for controlled scripts, preserve the file store and leave no call-scoped containers |
| Host dependency fixture | Pinned host module discovery and managed-assembly loading succeed; supplied module shadowing does not execute authored code |
| Clean wheel install | The built wheel installs with published dependencies and passes the installed-package smoke check outside the source tree |
| Workspace sample | Both tools report the expected findings; Bash reports its inline suppression |

## Remaining evidence

ACAS live qualification requires a configured sandbox group and imported candidate image; it has not been run for this change. The repository contains an opt-in ACAS tool test. Hosted CI, image vulnerability scanning, image signing and publication are separate gates and are not established by these local results. The `shell` profile is registered with the existing build, scan, approval and completion-record workflows; no release or deployment is performed by this change.
