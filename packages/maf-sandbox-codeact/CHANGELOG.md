# Changelog

## [0.12.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.11.0...maf-sandbox-codeact-v0.12.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* both kinds declare untrusted rather than leaving it to a tier neither controls ([#887](https://github.com/sokolaidev/maf-extensions/issues/887))

### Features

* **codeact:** a withheld result names the folder its outputs landed in, not which names landed ([#901](https://github.com/sokolaidev/maf-extensions/issues/901)) ([f00ab8a](https://github.com/sokolaidev/maf-extensions/commit/f00ab8adefd7d4826cb52de0f96bce6a8783809b))


### Fixes

* both kinds declare untrusted rather than leaving it to a tier neither controls ([#887](https://github.com/sokolaidev/maf-extensions/issues/887)) ([3167e8f](https://github.com/sokolaidev/maf-extensions/commit/3167e8f40f2134fc70825f11fa1b9fb4dc89c989))
* **codeact:** a withheld result says only whether the program exited cleanly, never the code or the stream sizes ([#899](https://github.com/sokolaidev/maf-extensions/issues/899)) ([25ffd97](https://github.com/sokolaidev/maf-extensions/commit/25ffd9723620914bfb6cbf56b7b324a230706a59))

## [0.11.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.10.0...maf-sandbox-codeact-v0.11.0) (2026-09-04)


### ⚠ BREAKING CHANGES

* CallerContext.list_files now answers with ListedFile entries. A kind reading store.read directly is unaffected and stays unlabelled; SandboxToolSession.read_file is the surface that carries the label.
* **codeact:** with withhold_guest_output=True, execute_code answers with two Content items rather than one string, and the recovery-route sentence is the second item rather than the last line of the first. A host that reads or asserts on the tool result's text should expect both items; a host that only runs an agent is unaffected.

### Features

* a kind is handed the labels of the files it was named, and reads them as labelled items ([#876](https://github.com/sokolaidev/maf-extensions/issues/876)) ([796e03b](https://github.com/sokolaidev/maf-extensions/commit/796e03bcd4ffc07fa3fd55e7a3e953d204a35e4a))
* a timed-out run's reason for reading no output is an attribute, and a withheld result carries it ([#844](https://github.com/sokolaidev/maf-extensions/issues/844)) ([5698da1](https://github.com/sokolaidev/maf-extensions/commit/5698da16471dcd1c96cc25fc3d6451c016313500))
* **codeact:** the withheld route sentence is its own trusted item, so hiding leaves it readable ([#857](https://github.com/sokolaidev/maf-extensions/issues/857)) ([435be26](https://github.com/sokolaidev/maf-extensions/commit/435be267b53abc856b288f366548511970dd78e8))


### Fixes

* **codeact:** a withheld tool tells the model nothing it writes to stdout or stderr comes back ([#837](https://github.com/sokolaidev/maf-extensions/issues/837)) ([66e0d78](https://github.com/sokolaidev/maf-extensions/commit/66e0d786b92697c2e4bcee286e4a89020496a97c))
* **codeact:** who owns stderr comes from the result, so a merging backend renders correctly too ([#851](https://github.com/sokolaidev/maf-extensions/issues/851)) ([6a1dc0a](https://github.com/sokolaidev/maf-extensions/commit/6a1dc0a7a36f9fc8ef18f9c227512b4cb03dbc1b))


### Documentation

* an undeclared result costs the model's sight of it, not the host's sinks ([#834](https://github.com/sokolaidev/maf-extensions/issues/834)) ([f0b8429](https://github.com/sokolaidev/maf-extensions/commit/f0b8429dc46034a27fbc76725c9cf4de1ef60b73))

## [0.10.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.9.2...maf-sandbox-codeact-v0.10.0) (2026-09-02)


### ⚠ BREAKING CHANGES

* **codeact:** a withheld result declares no source_integrity, since a model-authored program chose its values ([#816](https://github.com/sokolaidev/maf-extensions/issues/816))
* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789))

### Features

* a kind can know which tool call arguments the framework rewrote ([#827](https://github.com/sokolaidev/maf-extensions/issues/827)) ([c83160e](https://github.com/sokolaidev/maf-extensions/commit/c83160ec727f2a94e7facf46e3584a7984407fa6))
* a refusal names a rejected file by its position rather than echoing a value that is not a name ([#818](https://github.com/sokolaidev/maf-extensions/issues/818)) ([5123e52](https://github.com/sokolaidev/maf-extensions/commit/5123e52321766a70d079e26d369116cdf912fbfe))
* require maf-sandbox 0.30.0 in the dependents and 0.29 in the samples, and admit 0.31 ([#789](https://github.com/sokolaidev/maf-extensions/issues/789)) ([670a005](https://github.com/sokolaidev/maf-extensions/commit/670a0055f180c83aae50179055dd84b01bbca0f5))


### Fixes

* a name that landed is not repeated either, and two ways past the bound are closed ([#831](https://github.com/sokolaidev/maf-extensions/issues/831)) ([d430e78](https://github.com/sokolaidev/maf-extensions/commit/d430e7883ef5c1008a964e7d8f3b76bff95b4022))
* **codeact:** a withheld result declares no source_integrity, since a model-authored program chose its values ([#816](https://github.com/sokolaidev/maf-extensions/issues/816)) ([f15c23d](https://github.com/sokolaidev/maf-extensions/commit/f15c23de33f6e860d5415b208a643b45c7370086))
* **codeact:** the call-directory prefix is no longer 13 bytes ([#828](https://github.com/sokolaidev/maf-extensions/issues/828)) ([1d02da6](https://github.com/sokolaidev/maf-extensions/commit/1d02da61cc59654e8692c62e990b2d8554ebb3ec))

## [0.9.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.9.1...maf-sandbox-codeact-v0.9.2) (2026-09-02)


### Documentation

* **sandbox:** an integrity value is a label or a level, and "tier" is retired for it ([#813](https://github.com/sokolaidev/maf-extensions/issues/813)) ([e58ac51](https://github.com/sokolaidev/maf-extensions/commit/e58ac51ba1d7164b2a6dfede698c635a32db4930))

## [0.9.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.9.0...maf-sandbox-codeact-v0.9.1) (2026-09-01)


### Fixes

* every dependent admits maf-sandbox 0.29, and the samples floor on 0.28 ([#779](https://github.com/sokolaidev/maf-extensions/issues/779)) ([71c917a](https://github.com/sokolaidev/maf-extensions/commit/71c917a8d7ae9e253a30cb38e2fb25c393332fc1))

## [0.9.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.8.0...maf-sandbox-codeact-v0.9.0) (2026-09-01)


### Features

* **codeact:** withhold guest stdout/stderr and answer with host-generated text instead ([#771](https://github.com/sokolaidev/maf-extensions/issues/771)) ([008ba38](https://github.com/sokolaidev/maf-extensions/commit/008ba38ed98435ac88c1e98ec4d5476cfbdcac9a))

## [0.8.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.6...maf-sandbox-codeact-v0.8.0) (2026-08-29)


### ⚠ BREAKING CHANGES

* **sandbox:** a spec's identities are its surface's own, not a second declaration ([#720](https://github.com/sokolaidev/maf-extensions/issues/720))
* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681))

### Features

* a scope purge reports what it could not delete ([#681](https://github.com/sokolaidev/maf-extensions/issues/681)) ([739481d](https://github.com/sokolaidev/maf-extensions/commit/739481df3b7903c7f0015fee81282027666bc1ba))
* **sandbox:** a spec's identities are its surface's own, not a second declaration ([#720](https://github.com/sokolaidev/maf-extensions/issues/720)) ([5f08ca6](https://github.com/sokolaidev/maf-extensions/commit/5f08ca66f40b6460d3e660d4b4080b9ff03b0f56))


### Fixes

* admit maf-sandbox 0.27 in the dependents' range, and require 0.26 in the samples ([#748](https://github.com/sokolaidev/maf-extensions/issues/748)) ([f905461](https://github.com/sokolaidev/maf-extensions/commit/f9054614f061ffacf53abbbc174501f2d5be5a74))
* require maf-sandbox 0.27.0 in the dependents and 0.27 in the samples, and admit 0.28 ([#751](https://github.com/sokolaidev/maf-extensions/issues/751)) ([49d2a75](https://github.com/sokolaidev/maf-extensions/commit/49d2a758f60f3ffd503e5a94ea2b082c4d36ce9e))

## [0.7.6](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.5...maf-sandbox-codeact-v0.7.6) (2026-08-26)


### Fixes

* require maf-sandbox 0.25.0 in the dependents and 0.25 in the samples, and admit 0.26 ([#690](https://github.com/sokolaidev/maf-extensions/issues/690)) ([07f4a03](https://github.com/sokolaidev/maf-extensions/commit/07f4a0316acc74b0dc9a71f15dc4b9be943922bd))

## [0.7.5](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.4...maf-sandbox-codeact-v0.7.5) (2026-08-25)


### Fixes

* **codeact:** carry the host-tool surface on the spec so the router folds it ([#673](https://github.com/sokolaidev/maf-extensions/issues/673)) ([5fdbaf5](https://github.com/sokolaidev/maf-extensions/commit/5fdbaf54ced52f0ea4629cd3d514f24adb09542e))
* **codeact:** use new method names for host tool calls ([#635](https://github.com/sokolaidev/maf-extensions/issues/635)) ([0d424c5](https://github.com/sokolaidev/maf-extensions/commit/0d424c5af74be3d72ac5de0233332fdd632439d2))
* require maf-sandbox 0.24.0 in the dependents and 0.24 in the samples, and admit 0.25 ([#665](https://github.com/sokolaidev/maf-extensions/issues/665)) ([b410d73](https://github.com/sokolaidev/maf-extensions/commit/b410d73ac866f2abd19cf3e550f60f26920d5344))

## [0.7.4](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.3...maf-sandbox-codeact-v0.7.4) (2026-08-24)


### Fixes

* require maf-sandbox 0.22.0 in the dependents and 0.22 in the samples, and admit 0.23 ([#619](https://github.com/sokolaidev/maf-extensions/issues/619)) ([d8e122a](https://github.com/sokolaidev/maf-extensions/commit/d8e122a8f67e710704a4ffa0c11fdbebdaefb84e))
* require maf-sandbox 0.23.1 in the dependents and 0.23 in the samples, and admit 0.24 ([#652](https://github.com/sokolaidev/maf-extensions/issues/652)) ([f03d7f0](https://github.com/sokolaidev/maf-extensions/commit/f03d7f06d48a44079bc53d57337b06c5440870ae))

## [0.7.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.2...maf-sandbox-codeact-v0.7.3) (2026-08-23)


### Fixes

* **codeact:** fold the carries-out flag into sandbox_tool_declarations, now that its core is published ([#604](https://github.com/sokolaidev/maf-extensions/issues/604)) ([86a3e93](https://github.com/sokolaidev/maf-extensions/commit/86a3e93e35d6a1325bf91a014694f4df77248657))
* **codeact:** require the maf-sandbox release that added also_carries_out ([#583](https://github.com/sokolaidev/maf-extensions/issues/583)) ([2eac114](https://github.com/sokolaidev/maf-extensions/commit/2eac11451ef4653f6a8a4423c36ae7a2f68a1f09))
* require maf-sandbox 0.21.0 in the dependents and 0.21 in the samples, and admit 0.22 ([#596](https://github.com/sokolaidev/maf-extensions/issues/596)) ([1028a57](https://github.com/sokolaidev/maf-extensions/commit/1028a57e16d2fe5cb3aa0b3b948680e52fce90c3))

## [0.7.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.1...maf-sandbox-codeact-v0.7.2) (2026-08-22)


### Fixes

* **codeact:** fold the host-tool carries-out flag into sandbox_tool_declarations ([#582](https://github.com/sokolaidev/maf-extensions/issues/582)) ([7a04597](https://github.com/sokolaidev/maf-extensions/commit/7a04597a893a63a2500efb1605025c8a0ed157f6))
* require maf-sandbox 0.20.0 in the dependents and 0.20 in the samples, and admit 0.21 ([#564](https://github.com/sokolaidev/maf-extensions/issues/564)) ([727af26](https://github.com/sokolaidev/maf-extensions/commit/727af26c6db27a0de11a901d531f7183fda8426d))


### Reverts

* **codeact:** fold the host-tool carries-out flag into sandbox_tool_declarations ([#592](https://github.com/sokolaidev/maf-extensions/issues/592)) ([f533fe2](https://github.com/sokolaidev/maf-extensions/commit/f533fe22050a5eeae3e9b894d2fa833dabd6728a))

## [0.7.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.7.0...maf-sandbox-codeact-v0.7.1) (2026-08-22)


### Documentation

* restructure the sandbox documentation into a decided design and a research pipeline ([#527](https://github.com/sokolaidev/maf-extensions/issues/527)) ([4331f9c](https://github.com/sokolaidev/maf-extensions/commit/4331f9c238caa5e1b1f41b35ee29779d6b1ec17a))
* say what the 0.19 egress change breaks, and what to do about it ([#543](https://github.com/sokolaidev/maf-extensions/issues/543)) ([4b736de](https://github.com/sokolaidev/maf-extensions/commit/4b736de3368feb7fca0ffe36f2b607214d376d44))

## [0.7.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.6.1...maf-sandbox-codeact-v0.7.0) (2026-08-21)


### ⚠ BREAKING CHANGES

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530))

### Features

* backends declare egress_modes and kinds run a chosen egress mode ([#530](https://github.com/sokolaidev/maf-extensions/issues/530)) ([cc9a85f](https://github.com/sokolaidev/maf-extensions/commit/cc9a85f3155235e7a73fb5a14fcc79b696d37bd5))


### Fixes

* require maf-sandbox 0.19.0 in the dependents and 0.19 in the samples, and admit 0.20 ([#540](https://github.com/sokolaidev/maf-extensions/issues/540)) ([ae825c2](https://github.com/sokolaidev/maf-extensions/commit/ae825c2c8fd5e105402470c788b24371a77efa7c))

## [0.6.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.6.0...maf-sandbox-codeact-v0.6.1) (2026-08-21)


### Fixes

* **codeact:** a timeout no longer claims the program is still running ([#511](https://github.com/sokolaidev/maf-extensions/issues/511)) ([7bc3cd3](https://github.com/sokolaidev/maf-extensions/commit/7bc3cd322f989ac2c0991043ed97871928324129))
* **kinds:** wire kinds to framework-owned call directories ([#500](https://github.com/sokolaidev/maf-extensions/issues/500)) ([91fd4d1](https://github.com/sokolaidev/maf-extensions/commit/91fd4d18b3ef9dff5223713d26e78614eb847fb7))

## [0.6.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.5.1...maf-sandbox-codeact-v0.6.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* Sandbox.write_file now requires the keyword-only working_directory argument and refuses paths that escape it, pass through symlinked parents, target symlinks, or name the working directory itself.

### Features

* require a working directory for write_file and refuse paths that escape it ([#488](https://github.com/sokolaidev/maf-extensions/issues/488)) ([49795fa](https://github.com/sokolaidev/maf-extensions/commit/49795fa78a968451eef55fe27cd8784106f4ccc3))


### Fixes

* require maf-sandbox 0.18.0 in the dependents and 0.18 in the samples, and admit 0.19 ([#494](https://github.com/sokolaidev/maf-extensions/issues/494)) ([dd12d77](https://github.com/sokolaidev/maf-extensions/commit/dd12d7745b526052268b2124803e549b1e8c3d7f))

## [0.5.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.5.0...maf-sandbox-codeact-v0.5.1) (2026-08-19)


### Fixes

* require maf-sandbox 0.17.0 in the dependents and 0.17 in the samples, and admit 0.18 ([#472](https://github.com/sokolaidev/maf-extensions/issues/472)) ([dffd936](https://github.com/sokolaidev/maf-extensions/commit/dffd936ed3cb3c6a49d1dce0776ba321ee4d1dda))


### Documentation

* **samples:** a program in a sandbox calling back into the host, measured ([#433](https://github.com/sokolaidev/maf-extensions/issues/433)) ([23ba28a](https://github.com/sokolaidev/maf-extensions/commit/23ba28a50bfbed95df48bc81d03f0d63c9dfb014))
* two backends declare HOST_TOOLS, so stop saying none does ([#426](https://github.com/sokolaidev/maf-extensions/issues/426)) ([ca38e5c](https://github.com/sokolaidev/maf-extensions/commit/ca38e5ccfbb6d937fce3663b7aa6be6a3e528c35))

## [0.5.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.3...maf-sandbox-codeact-v0.5.0) (2026-08-17)


### Features

* **codeact:** a host can open named endpoints to a program, in two lists with different owners ([#420](https://github.com/sokolaidev/maf-extensions/issues/420)) ([d491728](https://github.com/sokolaidev/maf-extensions/commit/d491728ac260be20ff84aa5ab4518ee607ca14d0))
* **codeact:** a host-tool registry the program can call out to, refused until a backend serves it ([#373](https://github.com/sokolaidev/maf-extensions/issues/373)) ([5e7a253](https://github.com/sokolaidev/maf-extensions/commit/5e7a253d75d8d9d3ec63e2844a900eee337a67d1))


### Bug Fixes

* **codeact:** a reserved-name refusal names the file it collides with, and whether this tool writes or reads it ([#401](https://github.com/sokolaidev/maf-extensions/issues/401)) ([86506ae](https://github.com/sokolaidev/maf-extensions/commit/86506ae2191798bc77ce4fcb74d28ea3bcb2700c))
* require maf-sandbox 0.16.0 in the dependents and 0.16 in the samples, and admit 0.17 ([#386](https://github.com/sokolaidev/maf-extensions/issues/386)) ([7133401](https://github.com/sokolaidev/maf-extensions/commit/713340192dbc710c9c18f498a6615fc401332682))

## [0.4.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.2...maf-sandbox-codeact-v0.4.3) (2026-08-16)


### Bug Fixes

* admit maf-sandbox 0.16 in the dependents' range ([#358](https://github.com/sokolaidev/maf-extensions/issues/358)) ([0851c47](https://github.com/sokolaidev/maf-extensions/commit/0851c472294210956a53a670a5f324434d32bcd1))

## [0.4.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.1...maf-sandbox-codeact-v0.4.2) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.15 in the dependents' range ([#335](https://github.com/sokolaidev/maf-extensions/issues/335)) ([fc2ad7c](https://github.com/sokolaidev/maf-extensions/commit/fc2ad7c4f24edaa1a4b1c4501056195525de41b5))

## [0.4.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.4.0...maf-sandbox-codeact-v0.4.1) (2026-08-14)


### Bug Fixes

* admit maf-sandbox 0.14 in the dependents' range ([#316](https://github.com/sokolaidev/maf-extensions/issues/316)) ([c3777f0](https://github.com/sokolaidev/maf-extensions/commit/c3777f079ada6d6ee11502170e383513d54c6972))

## [0.4.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.3.0...maf-sandbox-codeact-v0.4.0) (2026-08-14)


### Features

* consolidate every work_dir onto /maf-sandbox/work ([#267](https://github.com/sokolaidev/maf-extensions/issues/267)) ([0f5c6c2](https://github.com/sokolaidev/maf-extensions/commit/0f5c6c2a91e611fbf58927618f848887cb2bc683))


### Bug Fixes

* require maf-sandbox 0.12.0 and admit 0.13 in the dependents' range ([#252](https://github.com/sokolaidev/maf-extensions/issues/252)) ([fb92562](https://github.com/sokolaidev/maf-extensions/commit/fb925620a4d6ad844512f34444bfafe04e81e827))

## [0.3.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.3...maf-sandbox-codeact-v0.3.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* "workspace" is gone from the public vocabulary; rename at the call site. `WorkspaceContext` is now `CallerContext`, and `make_workspace_context` is `make_caller_context` — whose first parameter is `list_files` rather than `store_walker`. `bicep_validate_tool` and `execute_code_tool` take `file_store=` where they took `workspace_store=`. `maf_sandbox_bicep.safe_workspace_path` is `safe_listed_path`. `work_dir` and `working_directory` are unchanged: they name the guest's working directory and were never this concept.

### Features

* retire "workspace" from the vocabulary — CallerContext, file_store, list_files ([#240](https://github.com/sokolaidev/maf-extensions/issues/240)) ([e746982](https://github.com/sokolaidev/maf-extensions/commit/e746982d42707e6c4599ba1ec927797b25d360e8))


### Bug Fixes

* require maf-sandbox 0.11.0 and admit 0.12 in the dependents' range ([#244](https://github.com/sokolaidev/maf-extensions/issues/244)) ([0968308](https://github.com/sokolaidev/maf-extensions/commit/096830831e4a5b0742206cdff8869ab0f3e4694c))

## [0.2.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.2...maf-sandbox-codeact-v0.2.3) (2026-08-12)


### Bug Fixes

* require maf-sandbox 0.10.0 and admit 0.11 in the dependents' range ([#231](https://github.com/sokolaidev/maf-extensions/issues/231)) ([353c1b3](https://github.com/sokolaidev/maf-extensions/commit/353c1b34f8c2d1d8f5f32dfa260913c27d50ab60))

## [0.2.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.1...maf-sandbox-codeact-v0.2.2) (2026-08-12)


### Bug Fixes

* admit maf-sandbox 0.10 in the dependents' range ([#219](https://github.com/sokolaidev/maf-extensions/issues/219)) ([f0b3f94](https://github.com/sokolaidev/maf-extensions/commit/f0b3f942f132cefeb35e4b2d90b98765d2905ffc))

## [0.2.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.2.0...maf-sandbox-codeact-v0.2.1) (2026-08-11)


### Bug Fixes

* require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range ([#194](https://github.com/sokolaidev/maf-extensions/issues/194)) ([cedc67c](https://github.com/sokolaidev/maf-extensions/commit/cedc67c504ec7785543222120ed08a56ad28062d))

## [0.2.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.3...maf-sandbox-codeact-v0.2.0) (2026-08-11)


### Features

* a workspace channel for CodeAct — files in, and outputs a portable backend can serve ([#158](https://github.com/sokolaidev/maf-extensions/issues/158)) ([322c579](https://github.com/sokolaidev/maf-extensions/commit/322c579fc8fa2a951af6777ada900d09cf30252f))

## [0.1.3](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.2...maf-sandbox-codeact-v0.1.3) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.8 in the dependents' range ([#179](https://github.com/sokolaidev/maf-extensions/issues/179)) ([8918fe8](https://github.com/sokolaidev/maf-extensions/commit/8918fe8dec6e7f076d448ae521c8a07634f5aa02))

## [0.1.2](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.1...maf-sandbox-codeact-v0.1.2) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.7 in the dependents' range ([#157](https://github.com/sokolaidev/maf-extensions/issues/157)) ([cb4d296](https://github.com/sokolaidev/maf-extensions/commit/cb4d296aa9b5af26a207f16632b63ec6640bbace))
* require maf-sandbox 0.7.0 in the packages that use it ([#170](https://github.com/sokolaidev/maf-extensions/issues/170)) ([4236d7c](https://github.com/sokolaidev/maf-extensions/commit/4236d7c9ab7086f7f0a4fa59771eb9d61f6eb04e))

## [0.1.1](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.1.0...maf-sandbox-codeact-v0.1.1) (2026-08-11)


### Bug Fixes

* admit maf-sandbox 0.6.0 in the dependents' range ([#150](https://github.com/sokolaidev/maf-extensions/issues/150)) ([f2e2ca1](https://github.com/sokolaidev/maf-extensions/commit/f2e2ca13d447f5885dc6d2b2d0b8d2f3e5bbb206))

## [0.1.0](https://github.com/sokolaidev/maf-extensions/compare/maf-sandbox-codeact-v0.0.1...maf-sandbox-codeact-v0.1.0) (2026-08-10)


### Features

* maf-sandbox-codeact — run agent-written Python in a sandbox, on any backend ([#97](https://github.com/sokolaidev/maf-extensions/issues/97)) ([f13b051](https://github.com/sokolaidev/maf-extensions/commit/f13b0515699e1239623a0969ed811d74483b2fbe))

## Changelog

All notable changes to `maf-sandbox-codeact` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet reached a stable API, so every release before `1.0.0` may include breaking changes.
