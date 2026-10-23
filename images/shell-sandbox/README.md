# Shell validation image

The `shell` release profile combines Bash, dash, ShellCheck, PowerShell and PSScriptAnalyzer in a Linux amd64 image. `image.json` pins each analyzer and interpreter by version and archive SHA256; the base image is pinned by digest. The normal image release pipeline builds, scans, signs and records this profile. Adding the profile does not publish an image.

```bash
docker build --platform linux/amd64 -t maf-shell:local images/shell-sandbox
docker run --rm -i --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,exec,size=512m maf-shell:local python3 - < images/shell-sandbox/test_runner.py
```

The image runs as root for the ACAS file plane. The kind requests closed egress and call-scoped disposal. The launcher reads a single JSON bundle, removes it, and supplies each source through anonymous pipes. Submitted `.psm1` files never become filesystem modules. PowerShell command discovery can load host-installed module code, so that code belongs to the host image's trust boundary.

PowerShell uses the default text-analysis rules. Module inputs also receive an in-memory AST pass for the pinned analyzer's two module-only script rules, `PSAvoidGlobalFunctions` and `PSAvoidGlobalAliases`, including their suppressions. The AST carries the manifest name with a canonical `.psm1` suffix; reports retain the original path. Source-byte encoding rules have no original file to inspect. Runtime errors and analyzer warning-stream messages from either pass make analysis incomplete. Recheck the module-only rule set when upgrading PSScriptAnalyzer.

## Host dependencies

Prepare dependencies during a derived image build. Copy approved modules into `/opt/maf-shell/dependencies/modules` and managed assemblies into `/opt/maf-shell/dependencies/assemblies`, including their own pinned transitive dependencies. Do not install dependencies at tool-call time. Run `python3 /opt/maf-shell/prepare_dependencies.py` in the build; it writes the complete file/hash inventory and prints its SHA256. Pass that digest as `dependency_inventory_sha256` to `make_shell_tools`. The launcher checks the tree against that inventory before analysis; symlinks and mismatches fail validation. The fixed PowerShell helper loads every approved managed assembly before parsing; conflicting assembly identities or load failures make analysis incomplete. Reference assemblies by their fixed absolute guest path. Profiles, user module directories and inherited environment variables are excluded.

The `fixtures/` derived image qualifies host module discovery, an assembly reference and a submitted module with the same name. It is test input, not a release profile:

```bash
docker build --build-arg BASE=maf-shell:local -t maf-shell:dependencies images/shell-sandbox/fixtures
docker run --rm -i --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,exec,size=512m maf-shell:dependencies python3 - < images/shell-sandbox/fixtures/test_dependencies.py
```

## ShellCheck audit adapter

ShellCheck's normal diagnostic output omits unused suppression declarations. `ShellAudit.hs` uses the pinned upstream parser to capture every recognized disable clause with its scope and rule ranges. A second analysis ignores only those parsed disable annotations to identify suppressed findings. Strings and heredoc bodies are not scanned as comments. Source reads resolve only against the in-memory manifest, trying the direct path before declared source paths and expanding a leading `SCRIPTDIR` component. Parent components resolve lexically within the manifest root; absolute paths and root escapes cannot select manifest files. `/dev/null` is modeled as an empty source. Sourced files are checked in each caller's dialect in both analysis passes. Ambient `.shellcheckrc` files and external source reads are disabled. Shell-changing directives that contradict the selected dialect and author changes to optional/extended analysis are refused.

The linked adapter is GPL-3.0-or-later. The image includes the upstream source archive, adapter source, parser patch and upstream license under `/usr/share/maf-shell` and `/usr/share/licenses/shellcheck`. Rebuild with this Dockerfile and the pinned archive; the patch fails if the parser shape changes. The Python kind is MIT and communicates with this separate executable using JSON.

## Evidence boundary

The offline image fixtures and repository Docker test exercise real analyzers. The ACAS test is opt-in with `MAF_SHELL_ACAS_IMAGE` plus the existing `ACAS_SANDBOX_*` configuration and a previously imported host image. Neither unit tests nor Docker results establish ACAS qualification, publication or deployment success.
