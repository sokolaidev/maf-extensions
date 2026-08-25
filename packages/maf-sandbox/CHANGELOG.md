# Changelog

All notable changes to `maf-sandbox` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.

## [0.24.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.23.1...maf-sandbox-v0.24.0) (2026-08-25)


### ⚠ BREAKING CHANGES

* **sandbox:** refuse a backend that cannot serve a workload's host-tool dispatch at attach ([#631](https://github.com/sokolaidev/maf-extensions/issues/631))

### Features

* **sandbox:** keep the dispatch names working beside the new host_tool_call ones ([#663](https://github.com/sokolaidev/maf-extensions/issues/663)) ([d4b20a3](https://github.com/sokolaidev/maf-extensions/commit/d4b20a357bfd06785ca12236be80e861c8d8248e))
* **sandbox:** refuse a backend that cannot serve a workload's host-tool dispatch at attach ([#631](https://github.com/sokolaidev/maf-extensions/issues/631)) ([9bcea42](https://github.com/sokolaidev/maf-extensions/commit/9bcea420f35477a89dc32b9a9d0fe9e01bcba3e5))

## [0.23.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.23.0...maf-sandbox-v0.23.1) (2026-08-24)


### Documentation

* the four versions tagged on 24 August never reached PyPI ([#646](https://github.com/sokolaidev/maf-extensions/issues/646)) ([2d35b50](https://github.com/sokolaidev/maf-extensions/commit/2d35b504c9b3e6f84943ef8fba4a9dd92a2c303c))

## [0.23.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.22.0...maf-sandbox-v0.23.0) (2026-08-24)

> **Correction, added after the release.** This version was tagged and a GitHub Release was created for it, but **it never reached PyPI** — so there is no `maf-sandbox` 0.23.0 to install. The publish run failed before the upload, on a repository test that read the tags of a shallow checkout ([#645](https://github.com/sokolaidev/maf-extensions/pull/645)); the tag records the right commit and no artifact was ever built from it.
>
> Release tags here cannot be moved, so this version number is spent rather than reused. **The code these entries describe ships in 0.23.1**, whose own section says so and is otherwise the same tree.
>
> The entries below are left in place: they are accurate about the commit, and deleting them would hide why this version exists at all.


### ⚠ BREAKING CHANGES

* **sandbox:** dispose a sandbox the framework could not clean, unless the host opts down ([#626](https://github.com/sokolaidev/maf-extensions/issues/626))
* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609))

### Features

* every backend serves reclaim, so a call's directory goes away without a shell ([#609](https://github.com/sokolaidev/maf-extensions/issues/609)) ([6fcfcf6](https://github.com/sokolaidev/maf-extensions/commit/6fcfcf6259874bbcb4f02ac23bddcc92ae6d8550))
* **sandbox:** dispose a sandbox the framework could not clean, unless the host opts down ([#626](https://github.com/sokolaidev/maf-extensions/issues/626)) ([55c8ff0](https://github.com/sokolaidev/maf-extensions/commit/55c8ff073b7f7c330268ce1fb03117db3936be1a))
* **sandbox:** tell a backend that predates reclaim what is missing, at acquire and at the call ([#636](https://github.com/sokolaidev/maf-extensions/issues/636)) ([92ff350](https://github.com/sokolaidev/maf-extensions/commit/92ff350cc2427aba029bed6fc22da94cc34098f8))

## [0.22.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.21.0...maf-sandbox-v0.22.0) (2026-08-23)


### Features

* **sandbox:** give HostToolRun a documented run_id for dispatch attribution ([#593](https://github.com/sokolaidev/maf-extensions/issues/593)) ([99e67a7](https://github.com/sokolaidev/maf-extensions/commit/99e67a7a47cdf2cabd270e10b69f3c633dcbeab5))
* **sandbox:** let a caller bound the guest program's stdout with output_limit ([#598](https://github.com/sokolaidev/maf-extensions/issues/598)) ([f538856](https://github.com/sokolaidev/maf-extensions/commit/f5388565a71eceee1606725b6e1b5d4b74d5dda5))
* **sandbox:** make the host-tool wire format a tested contract ([#590](https://github.com/sokolaidev/maf-extensions/issues/590)) ([62644e3](https://github.com/sokolaidev/maf-extensions/commit/62644e34985f96509a66a78c1c8f6aac96d96dec))


### Fixes

* **sandbox:** record a host-tool dispatch cancelled mid-effect ([#605](https://github.com/sokolaidev/maf-extensions/issues/605)) ([688e379](https://github.com/sokolaidev/maf-extensions/commit/688e379c84769ffb40eb8b3ea857ec84cdfc71c6))
* **sandbox:** step over a request number claimed by a worker that died mid-publish ([#613](https://github.com/sokolaidev/maf-extensions/issues/613)) ([c9b3668](https://github.com/sokolaidev/maf-extensions/commit/c9b3668be8e41b6b5b52feeeea9716e6c98b12a8))

## [0.21.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.20.0...maf-sandbox-v0.21.0) (2026-08-22)


### ⚠ BREAKING CHANGES

* **host-tools:** a registry is Identity.APP -only by default ([#572](https://github.com/sokolaidev/maf-extensions/issues/572))

### Features

* **host-tools:** a registry can restrict which identities its tools may exercise ([#570](https://github.com/sokolaidev/maf-extensions/issues/570)) ([24361d0](https://github.com/sokolaidev/maf-extensions/commit/24361d0d518775814e550f66def8d3518efdcd24))
* **host-tools:** a registry is Identity.APP -only by default ([#572](https://github.com/sokolaidev/maf-extensions/issues/572)) ([f7e82d5](https://github.com/sokolaidev/maf-extensions/commit/f7e82d5f36e07a77c3b1ce6eb0c147ac90dc6d74))
* **sandbox:** sandbox_tool_declarations can be told a workload carries out beyond the spec ([#581](https://github.com/sokolaidev/maf-extensions/issues/581)) ([7c9745d](https://github.com/sokolaidev/maf-extensions/commit/7c9745d49188e909882bb71641e54617264bcbd1))

## [0.20.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.19.0...maf-sandbox-v0.20.0) (2026-08-22)


### ⚠ BREAKING CHANGES

* **sandbox:** declare and match the guest's OS family, and give RUN_CODE its method ([#532](https://github.com/sokolaidev/maf-extensions/issues/532))
* remove the egress shim and Capability.NETWORK, finish samples and docs ([#534](https://github.com/sokolaidev/maf-extensions/issues/534))

### Features

* remove the egress shim and Capability.NETWORK, finish samples and docs ([#534](https://github.com/sokolaidev/maf-extensions/issues/534)) ([f496a48](https://github.com/sokolaidev/maf-extensions/commit/f496a48463799564644e16452506a8659819da12))
* **sandbox:** a shared egress-enforcement conformance probe ([#547](https://github.com/sokolaidev/maf-extensions/issues/547)) ([783b469](https://github.com/sokolaidev/maf-extensions/commit/783b469ed41d5b04f21887ca101c42d1dcd33c19))
* **sandbox:** declare and match the guest's OS family, and give RUN_CODE its method ([#532](https://github.com/sokolaidev/maf-extensions/issues/532)) ([f031325](https://github.com/sokolaidev/maf-extensions/commit/f0313251e64d2f1f5f26117d0b400c5a7763686e))


### Documentation

* restructure the sandbox documentation into a decided design and a research pipeline ([#527](https://github.com/sokolaidev/maf-extensions/issues/527)) ([4331f9c](https://github.com/sokolaidev/maf-extensions/commit/4331f9c238caa5e1b1f41b35ee29779d6b1ec17a))
* **sandbox:** repoint the eleven references the docs restructure left dangling ([#555](https://github.com/sokolaidev/maf-extensions/issues/555)) ([361225a](https://github.com/sokolaidev/maf-extensions/commit/361225aa0e43a36410189d4c1eed6271595190e9))
* say what 0.20 breaks for a backend author, before it publishes rather than after ([#549](https://github.com/sokolaidev/maf-extensions/issues/549)) ([e95b200](https://github.com/sokolaidev/maf-extensions/commit/e95b200cb3c7c6b092650a1cd3bb8826af737124))
* say what the 0.19 egress change breaks, and what to do about it ([#543](https://github.com/sokolaidev/maf-extensions/issues/543)) ([4b736de](https://github.com/sokolaidev/maf-extensions/commit/4b736de3368feb7fca0ffe36f2b607214d376d44))

## [0.19.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.18.1...maf-sandbox-v0.19.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))
* **sandbox:** a backend that declares no egress is refused as undeclared, not as unrestricted ([#521](https://github.com/sokolaidev/maf-extensions/issues/521)) ([f1adf39](https://github.com/sokolaidev/maf-extensions/commit/f1adf39f4a63ec9e116489d8f47e48fa489507a5))
* **sandbox:** workload-backend resolution of applicable egress mode ([#528](https://github.com/sokolaidev/maf-extensions/issues/528)) ([7f4d1f6](https://github.com/sokolaidev/maf-extensions/commit/7f4d1f62091f76158151bd2033debb249c449525))

## [0.18.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.18.0...maf-sandbox-v0.18.1) (2026-08-21)


### Fixes

* **core:** reclaim sandbox call directories after workload calls ([#496](https://github.com/sokolaidev/maf-extensions/issues/496)) ([15a9391](https://github.com/sokolaidev/maf-extensions/commit/15a9391f744740abf4078e12e2fa43927b3e4734))


### Performance

* **sandbox:** reduce host-tool transport latency for concurrent guest calls ([#504](https://github.com/sokolaidev/maf-extensions/issues/504)) ([6493081](https://github.com/sokolaidev/maf-extensions/commit/64930819219f14f21260b39ce0f5d56cb1b4ab1d))


### Documentation

* **core:** add the missing 0.18 upgrade notes and say which release the call reclaim shipped in ([#508](https://github.com/sokolaidev/maf-extensions/issues/508)) ([deef8aa](https://github.com/sokolaidev/maf-extensions/commit/deef8aa95721ab2895d1ae3d338535aa3c946373))
* **sandbox:** the protocol's delete is capability-gated, not absent ([#514](https://github.com/sokolaidev/maf-extensions/issues/514)) ([e5853da](https://github.com/sokolaidev/maf-extensions/commit/e5853da0d3d57382571f7290305f45900d3a4870))

## [0.18.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.17.0...maf-sandbox-v0.18.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))
* **sandbox:** a tool call owns a guest path, and the framework reclaims it when the call ends ([#481](https://github.com/sokolaidev/maf-extensions/issues/481)) ([c6748cb](https://github.com/sokolaidev/maf-extensions/commit/c6748cb2a5f8e8a2306986e634bd70ce9721b2e1))

## [0.17.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.16.0...maf-sandbox-v0.17.0) (2026-08-19)


### ⚠ BREAKING CHANGES

* **host-tools:** a stopped program takes its children with it where the guest can ([#455](https://github.com/sokolaidev/maf-extensions/issues/455))
* **protocol:** `Sandbox` gains `remove(path, *, working_directory, recursive=False)`. An implementation that does not define it no longer satisfies the protocol. Backends that cannot confine a removal should raise `NotImplementedError` and not declare `Capability.FILES_DELETE`, as `maf-sandbox-wslc` does.
* **host-tools:** a dispatched program that overruns is signalled, and the transport reclaims its own files ([#434](https://github.com/sokolaidev/maf-extensions/issues/434))

### Features

* **host-tools:** a dispatched program that overruns is signalled, and the transport reclaims its own files ([#434](https://github.com/sokolaidev/maf-extensions/issues/434)) ([505f986](https://github.com/sokolaidev/maf-extensions/commit/505f9862594dcca7fe53757a57615932ad6c3d48))
* **host-tools:** a registry observes each dispatch, with the run that made it ([#464](https://github.com/sokolaidev/maf-extensions/issues/464)) ([d502146](https://github.com/sokolaidev/maf-extensions/commit/d502146bf0479b441d7642007c664081aee0a834))
* **host-tools:** a stopped program takes its children with it where the guest can ([#455](https://github.com/sokolaidev/maf-extensions/issues/455)) ([c376cfc](https://github.com/sokolaidev/maf-extensions/commit/c376cfcbaae481fbf97e0f6806b42b4ac2d89f68))
* **protocol:** a sandbox can be asked to delete what a workload put there ([#452](https://github.com/sokolaidev/maf-extensions/issues/452)) ([2453820](https://github.com/sokolaidev/maf-extensions/commit/245382036ba1e2ddc18dea79b8e97d2cfb561935))
* **sandbox:** probes for every capability a backend claims, and CI that enumerates backends rather than listing them ([#462](https://github.com/sokolaidev/maf-extensions/issues/462)) ([f0915c7](https://github.com/sokolaidev/maf-extensions/commit/f0915c71819c729cd33aa130749fffc8d69fa377))


### Documentation

* SandboxSpec.image is backend-resolved, so core stops stating one backend's rule as the rule ([#430](https://github.com/sokolaidev/maf-extensions/issues/430)) ([002591f](https://github.com/sokolaidev/maf-extensions/commit/002591f55bf8f781d96b7b7ccf2960b24e22dd1e))
* two backends declare HOST_TOOLS, so stop saying none does ([#426](https://github.com/sokolaidev/maf-extensions/issues/426)) ([ca38e5c](https://github.com/sokolaidev/maf-extensions/commit/ca38e5ccfbb6d937fce3663b7aa6be6a3e528c35))

## [0.16.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.15.0...maf-sandbox-v0.16.0) (2026-08-17)


### ⚠ BREAKING CHANGES

* a run is two directories now, some run paths and program names are refused, and its timeout gets a type ([#376](https://github.com/sokolaidev/maf-extensions/issues/376))

### Features

* a run is two directories now, some run paths and program names are refused, and its timeout gets a type ([#376](https://github.com/sokolaidev/maf-extensions/issues/376)) ([bac426f](https://github.com/sokolaidev/maf-extensions/commit/bac426f2926d59621b41ef33002362e3658901cf))
* a separate-OS-process rung between runtime and container, spelled os_process ([#347](https://github.com/sokolaidev/maf-extensions/issues/347)) ([7269830](https://github.com/sokolaidev/maf-extensions/commit/72698303314a271f31cf4f26851dfae12cad0b2f))

## [0.15.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.14.0...maf-sandbox-v0.15.0) (2026-08-16)


### Features

* a host-tool transport an EXEC backend can implement, over request and response files ([#327](https://github.com/sokolaidev/maf-extensions/issues/327)) ([680abb0](https://github.com/sokolaidev/maf-extensions/commit/680abb0855a2396723c084c071bc2e2c7a164acb))


### Documentation

* the ladder one-liner still named the renamed bottom rung ([#348](https://github.com/sokolaidev/maf-extensions/issues/348)) ([76a175c](https://github.com/sokolaidev/maf-extensions/commit/76a175c8126c5585b4746cb4419ce686eb7e05fb))

## [0.14.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.13.0...maf-sandbox-v0.14.0) (2026-08-14)


### ⚠ BREAKING CHANGES

* rename Isolation.PROCESS to NONE, and reserve the old spelling ([#331](https://github.com/sokolaidev/maf-extensions/issues/331))

### Features

* rename Isolation.PROCESS to NONE, and reserve the old spelling ([#331](https://github.com/sokolaidev/maf-extensions/issues/331)) ([647e7a2](https://github.com/sokolaidev/maf-extensions/commit/647e7a2bd3d72abf0c2cf13ed2c8172dccfdfc32))

## [0.13.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.12.0...maf-sandbox-v0.13.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* refuse a conformance subject that declares no FILES_OUT, and hold every backend that serves it to the suite ([#298](https://github.com/sokolaidev/maf-extensions/issues/298)) ([ce855a5](https://github.com/sokolaidev/maf-extensions/commit/ce855a525ec0a5ba0bd8a0e231b3f93d3c99c1b2))

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.11.0...maf-sandbox-v0.12.0) (2026-08-12)


### Features

* package the three app-facing helpers every sample wrote for itself ([#239](https://github.com/sokolaidev/maf-extensions/issues/239)) ([a0e77c0](https://github.com/sokolaidev/maf-extensions/commit/a0e77c0924d36bd8515eeb939f6b2a3e70e2ec4d))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.10.0...maf-sandbox-v0.11.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* "workspace" is gone from the public vocabulary; rename at the call site. `WorkspaceContext` is now `CallerContext`, and `make_workspace_context` is `make_caller_context` — whose first parameter is `list_files` rather than `store_walker`. `bicep_validate_tool` and `execute_code_tool` take `file_store=` where they took `workspace_store=`. `maf_sandbox_bicep.safe_workspace_path` is `safe_listed_path`. `work_dir` and `working_directory` are unchanged: they name the guest's working directory and were never this concept.

### Features

* retire "workspace" from the vocabulary — CallerContext, file_store, list_files ([#240](https://github.com/sokolaidev/maf-extensions/issues/240)) ([e746982](https://github.com/sokolaidev/maf-extensions/commit/e746982d42707e6c4599ba1ec927797b25d360e8))

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.9.0...maf-sandbox-v0.10.0) (2026-08-12)


### Features

* name a symlink in the protocol, and hold every FILES_OUT backend to one conformance suite ([#215](https://github.com/sokolaidev/maf-extensions/issues/215)) ([051c4c2](https://github.com/sokolaidev/maf-extensions/commit/051c4c2a906dcca4f5bd45654834061befc71308))


### Documentation

* give the FILES_OUT pull surface a section an app author can start from ([#230](https://github.com/sokolaidev/maf-extensions/issues/230)) ([b41dd80](https://github.com/sokolaidev/maf-extensions/commit/b41dd8026b7ebb7f471f508df33877dce3c028d8))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.8.0...maf-sandbox-v0.9.0) (2026-08-12)


### Features

* a shared maf_sandbox.paths for guest-path confinement and the ancestor walk ([#216](https://github.com/sokolaidev/maf-extensions/issues/216)) ([9e99c2a](https://github.com/sokolaidev/maf-extensions/commit/9e99c2aa18b83041af4166903157b73975cee195))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.7.0...maf-sandbox-v0.8.0) (2026-08-11)


### Features

* the host-tools safety contract lands before anything can dispatch ([#133](https://github.com/sokolaidev/maf-extensions/issues/133)) ([#155](https://github.com/sokolaidev/maf-extensions/issues/155)) ([159a2a5](https://github.com/sokolaidev/maf-extensions/commit/159a2a5dfb3b454450ce634858721afa6c8db105))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.6.1...maf-sandbox-v0.7.0) (2026-08-11)


### Features

* let a workload name its artifacts at call time, and land them under a name of their own ([#156](https://github.com/sokolaidev/maf-extensions/issues/156)) ([53edc4d](https://github.com/sokolaidev/maf-extensions/commit/53edc4db92b4c788864f6b9781514b60d8ae2a00))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.6.0...maf-sandbox-v0.6.1) (2026-08-11)


### Documentation

* keep the sandbox protocol platform-neutral so the guest-platform axis lands additively ([#145](https://github.com/sokolaidev/maf-extensions/issues/145)) ([31906e8](https://github.com/sokolaidev/maf-extensions/commit/31906e8e9ea5e5044dc800acdd97a58b84cf179f))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.5.0...maf-sandbox-v0.6.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113))

### Features

* implement the FILES_OUT protocol surface and artifact landing ([#113](https://github.com/sokolaidev/maf-extensions/issues/113)) ([92e7a0f](https://github.com/sokolaidev/maf-extensions/commit/92e7a0f22cc4c3f757218d5ec2ae58a41d912f1f))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.4.0...maf-sandbox-v0.5.0) (2026-08-10)


### ⚠ BREAKING CHANGES

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96))

### Features

* replace deployed with a minimum-isolation floor, and match declared capabilities ([#96](https://github.com/sokolaidev/maf-extensions/issues/96)) ([b5990ee](https://github.com/sokolaidev/maf-extensions/commit/b5990ee492bca09a0e267172216c087be1db647a))


### Documentation

* tell 0.4.x users what to change for the isolation floor ([#101](https://github.com/sokolaidev/maf-extensions/issues/101)) ([5d57d01](https://github.com/sokolaidev/maf-extensions/commit/5d57d01c656883181d5fbccc21532cd85f3b4c4d))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.3.0...maf-sandbox-v0.4.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87))

### Bug Fixes

* a sandbox belongs to (key, kind) — two kinds on one agent never share one ([#87](https://github.com/sokolaidev/maf-extensions/issues/87)) ([fa321cf](https://github.com/sokolaidev/maf-extensions/commit/fa321cf53f643f9e30df910fc8e46c6a938d6605))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.2...maf-sandbox-v0.3.0) (2026-08-09)


### ⚠ BREAKING CHANGES

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73))

### Features

* rename maf-sandbox-aca to maf-sandbox-acas ([#73](https://github.com/sokolaidev/maf-extensions/issues/73)) ([c1f3395](https://github.com/sokolaidev/maf-extensions/commit/c1f3395e6be0c050e65365bdfdf1a65a22bac442))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.1...maf-sandbox-v0.2.2) (2026-08-09)


### Bug Fixes

* **aca:** stop two concurrent calls for one key from creating two sandboxes ([#58](https://github.com/sokolaidev/maf-extensions/issues/58)) ([b3641ea](https://github.com/sokolaidev/maf-extensions/commit/b3641ea299e9805039cdbad3b3ab4745a6d40f39))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.2.0...maf-sandbox-v0.2.1) (2026-08-08)


### Documentation

* stop naming the current version in the experimental notices ([#52](https://github.com/sokolaidev/maf-extensions/issues/52)) ([a358066](https://github.com/sokolaidev/maf-extensions/commit/a35806659b5da63b144e111e2d5e8023f0e00adc))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.1.1...maf-sandbox-v0.2.0) (2026-08-08)


### ⚠ BREAKING CHANGES

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40))

### Features

* backends declare what egress they can enforce, and a workload refuses one that cannot ([#40](https://github.com/sokolaidev/maf-extensions/issues/40)) ([4310250](https://github.com/sokolaidev/maf-extensions/commit/43102501bae173710fedddbb1ea7ab5a27e2def4))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-v0.1.0...maf-sandbox-v0.1.1) (2026-08-08)


### Documentation

* add a samples/ tree, starting with a one-turn Bicep validation agent ([#19](https://github.com/sokolaidev/maf-extensions/issues/19)) ([c2fa24d](https://github.com/sokolaidev/maf-extensions/commit/c2fa24d55b2e35b80ae027d6959067c6ebec1224))

## [0.1.0] - 2026-08-07

Initial extraction. `maf-sandbox` was split out of a production agent application's sandbox-routing module as the protocol seam between a host application and any sandbox provider: `Sandbox`, `SandboxBackend`, `SandboxKey`, `SandboxSpec`, `WorkspaceContext`, `SandboxRouter`, `SandboxPurger`, and the deployed-isolation rule (`DEPLOYED_ISOLATION`, `SandboxBackendNotPermitted`). This release also adds the publish-ready packaging metadata (license, classifiers, authors, self-contained tool configs) and the import-time experimental notice — no behavioral change to the protocol itself.
