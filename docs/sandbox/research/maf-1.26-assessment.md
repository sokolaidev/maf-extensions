# MAF 1.26 adoption

> Release assessment and adoption record. Current implementation and qualification evidence is recorded in [release compatibility](../../release-compatibility.md#maf-126-adoption).

Implementation tracks [#2641](https://github.com/sokolaidev/maf-extensions/issues/2641), following the merged 1.25 implementation in [#2643](https://github.com/sokolaidev/maf-extensions/pull/2643). The [current qualification and release sequence](../../release-compatibility.md#maf-126-adoption) separates current evidence from the retained assessment below.

## Current implementation

On main `b04514386`, the same 4,967-case selection passed 4,965 tests with two skips on 1.25 and failed 19 on the published 1.26 cohort; all 19 reproduced serially. Fifteen failures came from derived summaries and recall instructions being classified as new input by the finalized request snapshot. Four record-rewrite/budget failures came from recall retries loading already prepared history again. Request-boundary context attribution and the public context-provider middleware hook address these separately. No framework snapshot capture, replay check, budget, or grouping assertion is disabled.

Thirty-six new installed-framework cases exercise JSON-mode tool results, alias-aware structured responses, atomic session writes, path-error classification, skill collisions, evaluator refusals, instrumented model precedence, injected Mistral transport ownership, conservative replay, canonical approvals, exact service continuation, nested-agent state isolation and approval-message grouping. Four new stream/non-stream integration cases pin single-input/single-accepted-response persistence across retries, including caller input carrying forged summary metadata. These are offline tests, including the mock Mistral HTTP transport; they do not establish live provider qualification. The adoption record carries the complete local, installed-wheel and live MCP results and their limits.

Fresh resolution selects Core 1.26.0, OpenAI 1.18.2, Foundry 1.16.2, Mistral 1.0.0b261112 and Ollama 1.0.0b261112. Only these five third-party versions change. All twelve direct Core consumers use `>=1.26.0,<1.27`. Python and MCP bounds are unchanged. Pending 1.25 release PRs still prepare sandbox 0.55 and compaction 0.16, so the existing dependent ranges remain correct at this main revision; recheck them before merge if those releases land first.

## Retained pre-implementation assessment

The remainder is the assessment recorded on 2026-11-15. Its blockers, proposed versions, source revisions and incomplete-work statements are historical. Its 76-PR ledger remains the item-by-item source inventory; current implementation evidence appears above and in the adoption record. Published wheel hashes were verified again on 2026-11-21. The historical upstream tag/release endpoints returned 404 on that refresh, so the original GitHub inventory is retained evidence rather than a newly fetched tag comparison.


Assessment date: 2026-11-15. Expand [#2641](https://github.com/sokolaidev/maf-extensions/issues/2641) from the prepared 1.25 adoption to **Core 1.26 and its used provider cohort**. [#2643](https://github.com/sokolaidev/maf-extensions/pull/2643) remains the implementation baseline, with 1.25 code and new 1.26 analysis; it must not close the enlarged issue while 1.26 adaptation and qualification remain incomplete. The 1.24/1.25 sections above retain their historical evidence.

### 1.26 decision and source identity

**Do not admit 1.26 yet.** A controlled comparison on the same repository source passed 4,255 tests on 1.25 and failed 17 on 1.26. All 17 reproduced serially. This is a concrete compaction/history integration blocker, not just a need to update version strings. Keep the current PR's `<1.26` ceiling until the failures and the new security contracts are qualified; the intended eventual band is `>=1.26.0,<1.27`, without an unmeasured cross-minor promise.

| Identity | Assessed value |
|---|---|
| Upstream release | [python-1.26.0](https://github.com/microsoft/agent-framework/releases/tag/python-1.26.0), published 2026-11-13 08:28:16 UTC; tagged changelog section dated 2026-11-12 |
| Candidate tag commit | `4f2d146bda4a2068438406f394c65fda7effcff7` |
| Previous tag commit | `7bca1df23ea97b778dd5cd7a50d9db8d1ef832db` (python-1.25.0) |
| Complete commit inventory | [1.25...1.26 comparison](https://github.com/microsoft/agent-framework/compare/python-1.25.0...python-1.26.0): 76 commits, 76 primary PRs; 52 referenced by release notes and 24 additional PRs |
| Package batch | [Tagged manifest](https://github.com/microsoft/agent-framework/blob/4f2d146bda4a2068438406f394c65fda7effcff7/python/.github/release-python-1.26.0.json): 28 selected distributions, not every adapter |
| Tested repository source | PR #2643 at `ff29b49912d5b6b577a9715193cc55084c8bd6f4`, with locked 1.25 dependencies |
| Current main consulted for release planning | `54221d868ca09a8a48cad2a64732caa60ac9a2c9`; not the source used by the paired runtime tests |

The GitHub comparison's file list stops at 300 entries, so it is not the file-level inventory. Each of the 76 primary PRs was inspected through its own complete file list, including the second page of #10132. Primary PRs come from commit subjects; issue numbers and a historical related PR mentioned inside commit bodies are not extra changes in this release. The ledger below accounts for every primary PR and the six changelog bullets without PR links. The [tagged changelog](https://github.com/microsoft/agent-framework/blob/4f2d146bda4a2068438406f394c65fda7effcff7/python/CHANGELOG.md) and published wheels take precedence over development-branch documentation or draft PR descriptions.

### 1.26 candidate cohort and repository reach

| Distribution | Published candidate | Repository consequence |
|---|---|---|
| agent-framework-core | 1.26.0 | Coordinate all twelve direct consumers and the lock after adaptation; typing-extensions now requires `>=4.16,<5`, already satisfied in the measured environment |
| agent-framework-openai | 1.18.2 | Requires Core `>=1.26,<2`; qualify effective continuation identity, schema aliases, wrapper and direct client paths |
| agent-framework-foundry | 1.16.2 | Published Core floor remains 1.24; our coordinated candidate is 1.26. Rerun exports/typing and reasoning/replay contracts |
| agent-framework-mistral | 1.0.0b261112 | Explicit HTTPX `>=0.28.1,<1`; injected endpoint reporting and ownership need targeted coverage |
| agent-framework-ollama | 1.0.0b261112 | Adapter admits Ollama SDK `>=0.5.3,<0.7`; Cachebench's existing `<0.6` still intersects, so do not widen it without separate SDK evidence |

All five wheels declare Python `>=3.11`; this does not justify changing this repository's `>=3.12,<3.15`. MCP stays `==2.2.0`. Downloaded wheel SHA-256 values were checked against PyPI, actual imports/versions were recorded, and each candidate's active base requirements was checked against the overlay plus installed dependencies with no missing or incompatible base requirement. This is not a solve of repository metadata or all optional extras.

Seventeen Core Python files changed between the published wheels. In particular, **both `_sessions.py` and `_compaction.py` changed**; the byte-equality observation for 1.24-to-1.25 is inapplicable here. The direct private imports in compaction, its `group_messages` calls, session registry/persistence, `ToolResultRecallMiddleware`, Cachebench's `ConfiguredClient` and harness wiring, sandbox provenance/approval middleware, file stores and MCP client contracts determine priority. Unused adapter names alone cannot exclude a PR that also changes shared Core.

### 1.26 observed blockers and required contracts

**Finalized history capture changes recall persistence.** In upstream [#9877](https://github.com/microsoft/agent-framework/pull/9877), `PerServiceCallHistoryPersistingMiddleware` reuses the first prepared context and captures the finalized downstream request into new input/context lists before persisting the response. Our recall middleware inserts a temporary instruction and later removes it from its request lists; the new snapshot can retain that message independently. All four stream/composed variants of `test_harness_record_instruction_stays_out_of_history_and_cold_snapshots` now find that instruction in `AgentSession.to_dict()`. Fix the integration's transient-message/persistence boundary while preserving approved finalized content; do not disable FIDES or restore the old snapshot wholesale.

The other thirteen failures are in Cachebench's in-process compaction fixtures: eight terminate with `CompactionBudgetExceeded`, four observe changed summary/replay counts, and one no longer reaches the expected rewrite/merge steps. The budget failures include composed restart replay, latest-user replacement after restart, merged call/result integrity, preserved rewrites and target-budget behavior. They identify changed integration behavior, not eight proven independent upstream defects. Trace prepared-context reuse, attribution, message identity and finalized persistence before selecting an adaptation; increasing budgets or weakening expectations would hide the contract loss.

**Continuation authority is tied to the exact history.** #9877 normalizes client-declared continuation carriers, binds preparation and dispatch to the same effective identity, and verifies the serialized history snapshot. Partial, opaque, changed or malformed continuation cannot gain approval authority. Test local and service-managed histories, response-updated conversation identity, JSON/MessagePack reload, conflicting identity carriers, non-finite/cyclic/non-string-key values, cancellation and refused persistence. `SessionCompactionProvider`, `SessionPersistence` and `ConfiguredClient` must preserve the contract without manufacturing verification metadata.

**Replay and approval provenance remain conservative.** [#9876](https://github.com/microsoft/agent-framework/pull/9876) authenticates session-owned hidden placeholders and binds approval to the canonical stored call; [#9878](https://github.com/microsoft/agent-framework/pull/9878) distinguishes exact backend-restored history from fresh client tool results. The optional synchronous message labeler consumes authoritative provider source buckets and caches the exact message object within a run; it is not a new general provider lifecycle API. Keep it opt-in, place security after the providers it must classify, and never trust summaries or serialized labels solely because their source name looks familiar. Missing service history must remain untrusted/private. Exercise both provenance middleware orders and failed/refused store writes.

**Approval grouping changes our compaction unit.** [#9890](https://github.com/microsoft/agent-framework/pull/9890) changes Core grouping as well as Redis trimming: approval requests link to canonical calls, responses pair by occurrence, and the private output-pair helper is renamed. No repository import of the renamed helper or rejection-key constant was found, but anchored/user/tool summary strategies and shedding call `group_messages` directly. Qualify pending and resolved approval wrappers, reused call IDs, overlapping/reasoning groups and original input immutability. Trimming must not orphan approval, function call or result content.

Additional mandatory targeted cases are JSON-mode Pydantic tool results (#9036), alias-aware schemas (#9810), Windows session replacement failure and path classification (#10200/#10202), model option collision under instrumentation (#10258), skill filename collisions (#10158), evaluation refusals (#10180/#10176), and injected Mistral endpoint/ownership (#9968). Keep final-only harness output disabled by default (#10214). No legacy memory-store migration, new hosting server, new provider, or upstream Hyperlight/Monty/shell adoption is included.

### 1.26 release-note PR ledger

Each row has a source PR and a repository-specific disposition. “Qualify” is required work, not a claim that the new case already passed. Existing passing tests provide only the evidence listed below.

| Upstream PR | Change | Impact and adoption action |
|---|---|---|
| [#10214](https://github.com/microsoft/agent-framework/pull/10214) | Optional final-only non-streaming harness responses | Cachebench uses `create_harness_agent`; keep the default aggregate response. Qualify an opt-in separately because it changes benchmark observations; streaming is unchanged. |
| [#9876](https://github.com/microsoft/agent-framework/pull/9876) | FIDES replay and approval authority; optional message labeler | Mandatory: exercise forged/missing stamps, canonical pending calls, durable reload and both provenance middleware orders. A synchronous `message_labeler(source_id, message)` is explicit host policy, not permission to trust summaries. |
| [#9810](https://github.com/microsoft/agent-framework/pull/9810) | Copilot structured output and Core alias-aware JSON validation | Copilot is unused, but shared Core validation affects summary schemas. Add alias/name configuration and stream/non-stream schema cases; do not dismiss this as adapter-only. |
| [#10215](https://github.com/microsoft/agent-framework/pull/10215) | Task-scoped authority middleware sample | Design reference only. Do not add ambient or cross-task approval authority to the sandbox from a sample. |
| [#10156](https://github.com/microsoft/agent-framework/pull/10156) | Minimum compatible Copilot CLI version | No Copilot adapter/CLI integration here. No CLI floor change; upstream SDK/CLI compatibility is not a requirement on this repository's providers. |
| [#10152](https://github.com/microsoft/agent-framework/pull/10152) | Hyperlight interruption and default execution time limit | Upstream Python adapter is unused; our Hyperlight backend has independent transport/deadline contracts. No claim that this enforces our deadlines; reconcile its dependency floor with #10313. |
| [#10114](https://github.com/microsoft/agent-framework/pull/10114) | Lazy experimental AgentFactory in WorkflowFactory | No declarative WorkflowFactory consumption. No initialization change required. |
| [#9878](https://github.com/microsoft/agent-framework/pull/9878) | AG-UI replay provenance and shared Core approval/compaction handling | AG-UI transport is unused; shared Core changes are mandatory qualification. Preserve exact backend history provenance and conservative fresh tool results; require typing-extensions 4.16 Sentinel identity. |
| [#10168](https://github.com/microsoft/agent-framework/pull/10168) | Remove an invalid return-value docstring section | Upstream documentation only; no runtime adoption. |
| [#10203](https://github.com/microsoft/agent-framework/pull/10203) | Remove another invalid return-value docstring section | Upstream documentation only; no runtime adoption. |
| [#10233](https://github.com/microsoft/agent-framework/pull/10233) | Remove stale sample argument documentation | Upstream sample documentation only; no repository API change. |
| [#10239](https://github.com/microsoft/agent-framework/pull/10239) | Remove unused observability sentinel | No public behavior adopted; include changed Core observability in wrapper coverage. |
| [#10160](https://github.com/microsoft/agent-framework/pull/10160) | Add DXMT to community context-provider catalogue | Catalogue only; no new provider dependency or integration. |
| [#10185](https://github.com/microsoft/agent-framework/pull/10185) | Refocus upstream consumer skills | Upstream developer guidance, not installed runtime behavior. No local skill replacement. |
| [#10311](https://github.com/microsoft/agent-framework/pull/10311) | Match Python release titles to tag names | Release tooling only. Use the actual tag and published manifest for version identity. |
| [#10169](https://github.com/microsoft/agent-framework/pull/10169) | Remove nonexistent tools console entry point | We do not invoke that entry point or consume agent-framework-tools as our shell backend. No command migration. |
| [#10273](https://github.com/microsoft/agent-framework/pull/10273) | Deprecate legacy framework memory APIs | No matching legacy framework memory imports in shipped source. Our `testing.InMemoryStore` is unrelated. Do not migrate SessionPersistence/recall stores automatically; track upstream removal after 2026-12-31 if new usage appears. |
| [#10313](https://github.com/microsoft/agent-framework/pull/10313) | Lower Hyperlight dependency floors after interruption changes | Unused adapter. Installation with SDK 0.8 does not provide the SDK 0.9 interruption capability; neither floor proves this repository's native backend qualified. |
| [#9877](https://github.com/microsoft/agent-framework/pull/9877) | Bind FIDES to effective service continuation and exact prepared history | Primary blocker: changed per-service-call snapshot lifecycle meets our recall/compaction persistence. Repair the 17 failing cases without bypassing conservative continuation checks or deferred persistence. |
| [#10226](https://github.com/microsoft/agent-framework/pull/10226) | Isolate run-level approval state for agent tools | No shipped `as_tool()` call found, but shared Agent approval paths remain relevant. Add nested same-source rejection/distinct-source isolation and ensure private approval state never leaks into tool/MCP kwargs. |
| [#9036](https://github.com/microsoft/agent-framework/pull/9036) | Serialize Pydantic tool results in JSON mode | Qualify BaseModel results containing datetime/UUID and nested values through real tool middleware. Preserve existing SandboxResult rich Content and labels. |
| [#9907](https://github.com/microsoft/agent-framework/pull/9907) | Return no transcript results for non-positive limit | Core harness memory helper changes; add zero/negative-limit check if exposing transcript search. Existing recall stores are separate. |
| [#10180](https://github.com/microsoft/agent-framework/pull/10180) | Fail score threshold assertion when evaluator filter matches nothing | Core evaluation seam can affect benchmark validation. Cover no-match failure; never interpret an empty score set as success. |
| [#10176](https://github.com/microsoft/agent-framework/pull/10176) | Reject non-boolean passed values in evaluator dictionaries | Core evaluation seam: reject string/integer truthiness when score is also supplied; retain valid boolean cases. |
| [#10204](https://github.com/microsoft/agent-framework/pull/10204) | Close owned streams in functional workflow agents | No functional workflow wrapper in shipped source. Keep ConfiguredClient/ResponseStream cancellation tests; do not infer borrowed-stream ownership changes. |
| [#9944](https://github.com/microsoft/agent-framework/pull/9944) | Interpret naive checkpoint timestamps as UTC | Workflow checkpoint selection is not SessionPersistence. No store timestamp migration; no current checkpoint adapter adoption. |
| [#10200](https://github.com/microsoft/agent-framework/pull/10200) | Retry transient Windows atomic session replacement | Relevant to framework FileSessionStore and our persistence workflows. Test bounded retry/exhaustion, cancellation and unchanged prior bytes; this is not concurrent-writer or general transaction support. |
| [#10202](https://github.com/microsoft/agent-framework/pull/10202) | Classify actual Windows overlong-path failures correctly | Relevant to native harness file stores used by sandbox integration. Assert ENAMETOOLONG only after OS failure; do not impose a new blanket path-length limit or alter guest-path policy. |
| [#10158](https://github.com/microsoft/agent-framework/pull/10158) | Reject case-colliding skill resources/scripts | Core skills used by harness integrations. Add mixed-case collision fixture and ordinary skill loading; consider Windows and Linux consistency. |
| [#10170](https://github.com/microsoft/agent-framework/pull/10170) | Declare six existing orchestration exports | Typing export fix in unused orchestration package; no runtime adoption or new dependency. |
| [#10201](https://github.com/microsoft/agent-framework/pull/10201) | Declare existing Anthropic/Foundry exports | Foundry is consumed: rerun strict typing against candidate wheel. Anthropic integration is not selected; no new provider route. |
| [#10258](https://github.com/microsoft/agent-framework/pull/10258) | Avoid duplicate model keyword in observability wrapper | Relevant to ConfiguredClient defaults and client_kwargs. Assert one effective model reaches instrumentation and the leaf client; retain caller override precedence. |
| [#9890](https://github.com/microsoft/agent-framework/pull/9890) | Atomic Redis history trimming and shared approval grouping | Redis is unused, but Core `group_messages` is called throughout anchored/tool/user summary and shedding. Add pending/resolved approvals, repeated IDs, overlapping groups and reasoning; never split a linked group. |
| [#10039](https://github.com/microsoft/agent-framework/pull/10039) | Escape RediSearch query syntax | Redis memory adapter unused. No change to independent recall search or schema. |
| [#10251](https://github.com/microsoft/agent-framework/pull/10251) | Default untyped A2A raw parts to octet-stream | No A2A adapter. No automatic change to SandboxResult MIME contracts. |
| [#10249](https://github.com/microsoft/agent-framework/pull/10249) | Parse AG-UI SSE data without a following space | No AG-UI client. Existing MCP HTTP tests do not qualify this transport. |
| [#10183](https://github.com/microsoft/agent-framework/pull/10183) | Compare integral floats against integer Cosmos fields | No Cosmos collection adapter. No backend/filter migration. |
| [#9999](https://github.com/microsoft/agent-framework/pull/9999) | Prefer injected DocumentDB client over connection settings | No DocumentDB adapter. Preserve existing owned/borrowed-client checks for providers actually used. |
| [#10003](https://github.com/microsoft/agent-framework/pull/10003) | Include cache tokens in Bedrock/Claude input usage | These adapters are not selected. Do not alter Azure/OpenAI/Mistral/Ollama cache accounting from this fix; qualify those routes independently. |
| [#10295](https://github.com/microsoft/agent-framework/pull/10295) | Keep registered tool policy authoritative in declarative agents | No declarative agent factory. Relevant security design constraint, not evidence that sandbox callable metadata can override policy. |
| [#10112](https://github.com/microsoft/agent-framework/pull/10112) | Remove forbidden keep-alive header from DevUI SSE | No DevUI dependency or hosted endpoint. No server header change. |
| [#10207](https://github.com/microsoft/agent-framework/pull/10207) | Reject skip with key lookup in PostgreSQL/DuckDB collections | Neither adapter is consumed. No change to our persistence key semantics. |
| [#10259](https://github.com/microsoft/agent-framework/pull/10259) | Honor streaming function-argument merge mode in Responses hosting | Foundry hosting server is not our Foundry/OpenAI client adapter. No server adoption; keep client parser replay tests separate. |
| [#10213](https://github.com/microsoft/agent-framework/pull/10213) | Report Gemini prompt blocks as content_filter | Gemini is not a selected provider. No finish-reason translation added. |
| [#10188](https://github.com/microsoft/agent-framework/pull/10188) | Reject Gemini candidate_count other than one | Gemini unused; no new option validation in generic ConfiguredClient. |
| [#10284](https://github.com/microsoft/agent-framework/pull/10284) | Accept inline file_data in Responses hosting parser | Hosting parser unused. This does not prove client-side file replay or sandbox file-output support. |
| [#9911](https://github.com/microsoft/agent-framework/pull/9911) | Omit unset identity kwargs for mem0 OSS clients | mem0 unused. No identity/default policy change. |
| [#9968](https://github.com/microsoft/agent-framework/pull/9968) | Report injected Mistral client's configured endpoint | Used provider: verify `service_url`, injected client ownership and actual transport target with default/custom endpoints. Do not overwrite injected endpoint metadata. |
| [#10227](https://github.com/microsoft/agent-framework/pull/10227) | Require keyword arguments for Monty typed tools | Monty adapter unused; separate sandbox kinds do not inherit this rule. |
| [#10193](https://github.com/microsoft/agent-framework/pull/10193) | Correct per-message Purview user mapping | Purview unused. No host identity mapping change. |
| [#10257](https://github.com/microsoft/agent-framework/pull/10257) | Correct Purview replacement cache accounting | Purview unused. Unrelated to Cachebench prompt-cache measurements. |
| [#10229](https://github.com/microsoft/agent-framework/pull/10229) | Resolve Windows LocalShell tools from PATH only | Upstream LocalShellExecutor is not our `maf-sandbox-shell` backend. Preserve our explicit executable policy; no inferred protection or automatic switch. |

### 1.26 additional tag-range PR ledger

These 24 primary PRs are in the tag comparison but not referenced by the Python release notes. Their exclusions are based on changed files, not merely their titles.

| Upstream PR | Change | Repository disposition |
|---|---|---|
| [#10153](https://github.com/microsoft/agent-framework/pull/10153) | .NET: Fix initializer spelling in Cosmos test comment | Cosmos test comment; .NET only. |
| [#10154](https://github.com/microsoft/agent-framework/pull/10154) | .NET: Fix spelling in WorkflowBuilder XML comment | WorkflowBuilder XML comment; .NET only. |
| [#10157](https://github.com/microsoft/agent-framework/pull/10157) | .NET: chore: bumps release version for dotnet | .NET release version update; no Python distribution delta. |
| [#8437](https://github.com/microsoft/agent-framework/pull/8437) | .NET: clarify Harness client API selection | .NET Harness client API guidance; not our Python ConfiguredClient. |
| [#10191](https://github.com/microsoft/agent-framework/pull/10191) | Rename release-notes skill to draft-release-notes | Upstream maintainer skill rename; no runtime or local skill update. |
| [#9702](https://github.com/microsoft/agent-framework/pull/9702) | .NET: Correct misleading comments in FileMemoryProvider and AgentHooksFunctionMiddleware | .NET FileMemory/AgentHooks comments; not Python memory semantics. |
| [#10132](https://github.com/microsoft/agent-framework/pull/10132) | .NET: [BREAKING] Reduce Foundry Hosting API surface | .NET Foundry Hosting API reshape and related documentation; all 160 file entries inspected, no Python client adoption. |
| [#10211](https://github.com/microsoft/agent-framework/pull/10211) | .NET: [BREAKING] Align A2A extension naming and return types | .NET A2A API naming/types; no Python contract. |
| [#9790](https://github.com/microsoft/agent-framework/pull/9790) | .NET: Allow persisting partial interaction when streaming is cancelled | .NET partial persistence on cancellation; does not establish Python SessionPersistence behavior. |
| [#10231](https://github.com/microsoft/agent-framework/pull/10231) | .NET: Fix flaky MultiStepOperationTimeout unit test | .NET timeout test stabilization; no Python runtime change. |
| [#10175](https://github.com/microsoft/agent-framework/pull/10175) | .NET: Bump the aspire group with 2 updates | .NET Aspire dependency group; no Python cohort change. |
| [#10236](https://github.com/microsoft/agent-framework/pull/10236) | CI: Drop the path filter that matches no directory | Upstream CI path-filter cleanup; no repository workflow change. |
| [#10066](https://github.com/microsoft/agent-framework/pull/10066) | [BREAKING] .NET: fix(hyperlight): propagate cancellation and limit execution | .NET Hyperlight cancellation; independent of our native backend. |
| [#10217](https://github.com/microsoft/agent-framework/pull/10217) | .NET: [BREAKING] Clarify Foundry storage docs and user identity accessor | .NET Foundry storage/identity API and docs; no Python client ownership change. |
| [#10244](https://github.com/microsoft/agent-framework/pull/10244) | .NET: Scaffold Foundry Hosted Agents A2A project | .NET Foundry A2A scaffold; no selected Python hosting surface. |
| [#10225](https://github.com/microsoft/agent-framework/pull/10225) | .NET: Require full LocalCodeAct interpreter paths | .NET LocalCodeAct interpreter-path requirement; not our CodeAct kind. |
| [#10252](https://github.com/microsoft/agent-framework/pull/10252) | .NET: Fix loopback MCP test server shutdown race | .NET loopback MCP test shutdown; no Python MCP client fix. |
| [#10267](https://github.com/microsoft/agent-framework/pull/10267) | .NET: Update Aspire to 13.6.5 | .NET Aspire 13.6.5; no Python dependency change. |
| [#10255](https://github.com/microsoft/agent-framework/pull/10255) | .NET: Honor includeExceptionDetails for OpenAI Responses workflow failures | .NET Responses workflow error reporting; not our client replay path. |
| [#10254](https://github.com/microsoft/agent-framework/pull/10254) | .NET: Keep Foundry client headers scoped to each run | .NET Foundry per-run headers; no Python adapter behavior claim. |
| [#10266](https://github.com/microsoft/agent-framework/pull/10266) | .NET: [BREAKING] Require a fully qualified container runtime path for DockerShellExecutor | .NET DockerShell executable-path requirement; not our Docker backend. |
| [#10270](https://github.com/microsoft/agent-framework/pull/10270) | Python: Add Supermemory to the community projects catalog | Python community Supermemory catalogue entry only; no runtime provider. |
| [#10298](https://github.com/microsoft/agent-framework/pull/10298) | Python: Prepare 1.26.0 release | Release manifest, package floors/ceilings, explicit HTTPX, tooling and minimal lint/test updates. Assess six unlinked bullets below and published wheel metadata; no indiscriminate SDK/tool upgrade. |
| [#10192](https://github.com/microsoft/agent-framework/pull/10192) | .NET: [BREAKING] fix(dotnet): validate declarative MCP destinations | .NET declarative MCP destination validation; does not qualify our Python MCP service. |

### 1.26 unlinked release changes

All six unlinked changelog bullets are covered by release preparation [#10298](https://github.com/microsoft/agent-framework/pull/10298). These are distinct from the 52 linked PRs above.

| Item | Change | Repository decision |
|---|---|---|
| U1 | Exact umbrella Core pin and standard cohort, including Copilot 2.2 | Use the five consumed distributions in the candidate table. Do not install the umbrella's unused integrations |
| U2 | Core 1.26 floors for AG-UI, Copilot, OpenAI and Redis; other floors retained by API usage | OpenAI requires the new minor; direct suite consumers must move together. Older floors on other adapters do not prove suite cross-minor compatibility |
| U3 | Expanded SDK ceilings: Anthropic, Tenacity, DuckDB, Azure AI Projects, Google GenAI, Mistral, Monty, Ollama, Oracle and RedisVL | Preserve repository SDK choices where still satisfiable; independently qualify a newer SDK before broadening a local range. Do not conflate adapter uptake with every newly admitted SDK |
| U4 | Explicit Mistral HTTPX dependency and transport-independent ownership tests | Include HTTPX in the clean solve; verify injected client/transport ownership through our actual Mistral route |
| U5 | Upstream development/tool pins and DevUI watchdog refresh; Lab separate | No automatic upgrade of our Ruff, pytest or other tools; Lab is outside the 28-package batch |
| U6 | Minimal lint cleanups and public OpenAI shell-tool test API; stale Cosmos lint suppression | Review shared Core changes but do not migrate to a removed SDK private helper. Cosmos cleanup does not imply a new runtime package release |

### 1.26 verification completed

The two runs use the identical selection and source at `ff29b499`, Windows CPython 3.13.12 and the same transitive environment. Baseline uses the locked 1.25 cohort. Candidate uses hash-verified published wheels in an isolated directory placed first on `PYTHONPATH`; actual Core import location and all five distribution versions are logged. It deliberately bypasses the existing `<1.26` declarations, so it is not a supported consumer installation.

| Check | Result | Boundary |
|---|---|---|
| Baseline 1.25, four pytest workers | 4,255 passed, 2 skipped, 45 warnings; 128.17 seconds | Entire compaction and Cachebench suites plus selected framework/result/integrity/MCP contracts |
| Candidate 1.26, same selection and four workers | 4,238 passed, 17 failed, 2 skipped, 42 warnings; 138.15 seconds | Non-green; no claim that 1.26 is compatible |
| Candidate serial rerun of the 17 failures | 17 failed, 2 warnings; 20.62 seconds | Same failures reproduce without worker concurrency |
| Diagnostic only: bypass finalized-message capture inside one test process | 13 passed, 4 failed, 2 warnings; 17.00 seconds | Isolates sensitivity to the new snapshot behavior; deliberately disables a new contract and is not a fix or compatibility pass |
| Candidate wheel metadata/base dependencies | Five verified versions and hashes; active base requirements satisfied | Not a fresh resolution, complete extras test, wheel smoke install or publication gate |
| Core implementation comparison | 17 Python files differ, including both private modules consumed by compaction | Source evidence, not a passing private-API compatibility assertion |
| Assessment inventory and documentation | 76 unique PR rows reconcile with complete PR file lists; 52 note PRs, 24 extras, six unlinked bullets and 28 manifest packages; documentation paths, 262 structure tests and 430 tracker references pass | Analysis/documentation validation only |
| PR #2643 hosted checks observed before this assessment commit | 44 successful, 4 skipped at `ff29b499` | 1.25 head only; neither 1.26 nor a later assessment head is validated by these checks |

Selection: `packages/maf-compaction/tests`, `packages/maf-cachebench/tests`, sandbox `test_maf_adoption.py`, `test_result_contract.py`, `test_result_integrity.py`, and repository `test_framework_approval_contract.py`, `test_maf_124_compatibility.py`, `test_maf_125_compatibility.py`, `test_maf_125_provider_replay.py`, `test_mcp_workload_service.py`. The files named “live” in the failed Cachebench selection use in-process fixtures; these are not external model calls. Elapsed times are not a performance comparison.

The diagnostic replaces only `PerServiceCallHistoryPersistingMiddleware._capture_finalized_messages` with a no-op in the test process; no wheel or installed source is edited. The four remaining failures are the two merged call/result integrity fixtures, preserved rewrites and target-budget behavior. This narrows the investigation but does not attribute every failure to one upstream PR or justify removing the snapshot contract. An initial diagnostic attempt used an incorrect private class name and stopped before tests; the recorded result uses the verified class above.

Local evidence is retained under `.artifacts/maf-1.26-assessment/`: upstream inventory, complete per-PR files, candidate metadata and wheel hashes, changed-Core hashes, baseline/candidate logs, serial failure log and `diagnostic-no-finalized-capture.log`. Raw logs include local paths and must be sanitized before publication. No product/dependency/generated release files are changed by this additional assessment.

Not verified: 1.26 adaptations, the full repository gate on 1.26, a resolved lock or all provider extras, strict candidate typing, clean built-wheel installation, supported Python 3.12/3.14 or Linux, new adversarial cases listed above, current-main runtime compatibility, exact-new-head hosted CI, live model routes, native backend qualification, production stores, image publication or suite publication.

### 1.26 release planning against current main

The original sandbox 0.54 target is stale. At the main revision above, **sandbox 0.54.0 and compaction 0.13.4 still require Core `>=1.24,<1.25`**; both versions were verified on PyPI. Published Bicep 0.26.1 still caps sandbox below 0.54, while main has a newer range declaration. A source range on main is not a published dependent.

For a feature adoption on that snapshot, prepare **sandbox 0.55.0**, with all eighteen dependents on **`>=0.55.0,<0.56`**, and **compaction 0.14.0**, with Cachebench on **`>=0.14.0,<0.15`**. These are proposed release-derived requirements, not manual version edits or proof that future PyPI state will match. Recompute from actual main and final shipped changes before implementing. Re-run the published-dependent gate: the historical rejection of sixteen dependents against sandbox 0.53.2 is not a current 1.26 result.

Publish sandbox/compaction prerequisites before dependent uploads; ensure required Bicep/Docker sibling lines publish before MCP and compaction before Cachebench. Branch-wheel overrides qualify the branch combination only. Real upload gates must resolve wheel requirements from the index. Samples floors remain a separate later change after all referenced dependencies publish. No merge, tag, deployment approval, store migration or publication is part of this assessment.

### 1.26 ordered adoption and issue acceptance

| Order | Scope | Acceptance | State |
|---|---|---|---|
| P0 | Expand #2641 and retain the complete incremental assessment | 52 release-note PRs, 24 additional PRs, six unlinked bullets and published cohort reconciled; old evidence remains revision-scoped | Analysis complete |
| P1 | Adapt recall, history compaction and session persistence to finalized snapshots/prepared context | All 17 regressions pass on unmodified 1.26; transient recall prompts never persist; no duplicate compaction/summary cost, undone rewrite, orphaned group or hidden budget failure; stream/non-stream and warm/restarted sessions agree | Blocking |
| P2 | Qualify #9876/#9877/#9878/#10226 and shared grouping #9890 | Conservative replay/continuation and canonical approvals hold; refusal before side effects; nested authority isolated; unchanged prior durable bytes on denial/failure; approval/call/result groups indivisible | Required |
| P3 | Add targeted schema, serialization, file/session, model instrumentation, skills, evaluation and Mistral cases | Tests exercise installed Core/provider implementations and actual outgoing data; existing 1.25 FIDES/MCP/provider regressions retained | Required |
| P4 | Reconcile with latest main, then move all twelve Core bands to `>=1.26.0,<1.27`, consumed provider floors/extras and lock | Fresh solve; actual candidate versions logged; prepared suite floors recomputed and both ends moved; no generated versions/changelogs/manifest edits or premature samples floor | Required |
| P5 | Run complete local gate, docs/Markdown/tracker checks, build/metadata/clean-install/combined-install and published-core gates | Exact final revision, supported interpreters/platforms and hosted CI recorded; baseline failures separated; source, branch-wheel and published-wheel evidence distinguished | Required |
| P6 | Bounded live qualification, then maintainer release handoff | Record configured Azure Chat/Responses, Foundry, Mistral, Ollama/OpenRouter routes, MCP ownership and representative sandbox behavior; explicitly retain unavailable routes. Recheck prerequisite publication and samples order | Required; release remains maintainer-owned |

Retain the 1.25 implementation as the adaptation baseline, not as completion of the widened issue. The eventual implementation PR may restore a closing reference only when the 1.26 acceptance criteria and tracking rows reflect the work actually delivered.
