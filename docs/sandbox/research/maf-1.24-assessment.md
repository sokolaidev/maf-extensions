# Agent Framework Python release assessments

> Historical release assessments and adoption plans. Current implementation and qualification evidence is recorded in [release compatibility](../../release-compatibility.md).

Latest assessment: [MAF 1.25 and adoption plan](#maf-125-assessment-and-adoption-plan). The 1.24 record below preserves its original scope and evidence.

## MAF 1.24 original assessment

> Assessment and proposed adoption plan, completed on 2026-10-30. This record evaluates the tagged release against a pinned maf-extensions main snapshot; implementation and current validation are tracked in [#2342](https://github.com/sokolaidev/maf-extensions/issues/2342) and [release compatibility](../../release-compatibility.md#maf-124-adoption). The Proposed statuses below preserve the original assessment.

## Decision

Proceed with a coordinated, tested adoption of Core 1.24 and its used provider cohort. Existing behavior passed the selected 3,120-test suite on both the locked baseline and the candidate wheels. Do not widen package ceilings as an isolated metadata edit: qualify file-store behavior, provider message identity, client ownership and private compaction integration together. Keep agent-triggered compaction and skill unloading as separate opt-in work.

Two concrete integration gaps need explicit treatment: prefixed file-write tools are invisible to the default provenance name set unless registered with `also_observes`; and Cachebench's `ConfiguredClient` does not forward async-context special methods. Neither changes existing unprefixed wiring or makes ordinary client calls require a context. These are migration constraints and feature-adoption gaps, not evidence that all current calls fail on 1.24.

## Scope and source identity

- Upstream: [python-1.24.0 release](https://github.com/microsoft/agent-framework/releases/tag/python-1.24.0), commit `43d767c55050480e470ebf0a89caa4141d325ebf`.
- Authoritative change inventory: [tagged Python changelog](https://github.com/microsoft/agent-framework/blob/43d767c55050480e470ebf0a89caa4141d325ebf/python/CHANGELOG.md), 46 release bullets with 74 distinct PR references; repeated cross-package references are reconciled once below. Ten unlinked changelog entries and release-preparation changes are also assessed: **85 completed assessment items**. Completion means assessed, not implemented or production-qualified.
- Package cohort: [tagged release manifest](https://github.com/microsoft/agent-framework/blob/43d767c55050480e470ebf0a89caa4141d325ebf/python/.github/release-python-1.24.0.json), 43 independently versioned distributions. Older narrative in the release page is preparation history; the tag's files determine the assessment.
- Our baseline: [main at 375a60ad](https://github.com/sokolaidev/maf-extensions/tree/375a60ad7ad3f4c4379b4efe70cb02f266f276ff), verified against GitHub during this assessment. The active working branch was an older feature branch; assessment used a separate archive of current main without switching it.
- Publication verified from PyPI for [Core 1.24.0](https://pypi.org/project/agent-framework-core/1.24.0/), [OpenAI 1.18.0](https://pypi.org/project/agent-framework-openai/1.18.0/), [Foundry 1.16.0](https://pypi.org/project/agent-framework-foundry/1.16.0/), [Mistral 1.0.0b261030](https://pypi.org/project/agent-framework-mistral/1.0.0b261030/) and [Ollama 1.0.0b261030](https://pypi.org/project/agent-framework-ollama/1.0.0b261030/); their published wheels were installed into an isolated override directory. This does not assert publication of all 43 distributions.

## Dependency and code map

| Surface | Current main | Candidate / consequence |
|---|---|---|
| Core, directly in 11 packages | `>=1.23.0,<1.24`; locked 1.23.0 | Recommended qualified band `>=1.24.0,<1.25`; cannot currently select 1.24 |
| OpenAI, root dev and Cachebench extra | `>=1.17.0,<2`; locked 1.17.0 | 1.18.0, requiring Core >=1.24 |
| Foundry, root dev and Cachebench extra | `>=1.15.0,<2`; locked 1.15.0 | 1.16.0, requiring Core >=1.24 and OpenAI >=1.18 |
| Mistral, root dev and Cachebench extra | `>=1.0.0b261015,<2`; locked that beta | 1.0.0b261030, requiring Core >=1.24 |
| Ollama, root dev and Cachebench extra | `>=1.0.0b261022,<2`; locked that beta | 1.0.0b261030, requiring Core >=1.24; underlying SDK remains compatible with locked 0.5.3 |
| Python, all 20 local packages | `>=3.12,<3.15` | Already above new supported baseline; no floor change |
| MCP | `mcp==2.2.0` | Core's MCP extra still admits >=2.2.0,<3; no second major migration |

The eleven direct Core consumers are `maf-sandbox`, `maf-compaction`, `maf-cachebench`, `maf-sandbox-bicep`, `maf-sandbox-codeact`, `maf-sandbox-terraform`, `maf-sandbox-drawio`, `maf-sandbox-shell`, `maf-sandbox-document`, `maf-sandbox-mcp`, and the newly merged `maf-sandbox-azure-pricing`. The nine remaining packages must be tested through the suite but do not need a direct framework import/dependency added. Do not raise their sandbox floors merely because upstream released a new minor.

Evidence anchors for the item ledger:

- [Core framework glue and file-store registry](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-sandbox/src/maf_sandbox/maf.py): `file_store_provenance_middleware`, `ScopedFileStores`, outputs tools and sinks.
- [File provenance](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-sandbox/src/maf_sandbox/_file_provenance.py): `FILE_STORE_WRITE_TOOLS`, `store_key`, `FileStoreProvenance`.
- [Recall/private persistence hooks](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-compaction/src/maf_compaction/_toolsummary.py): `_CURRENT_RUN_IDENTITY`, `_DEFERRED_RUN_PERSISTENCE`, `_RunPersistenceGate`; their definitions are unchanged between the two upstream tags.
- [Compaction session binding](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-compaction/src/maf_compaction/_session.py), [whole-session persistence](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-compaction/src/maf_compaction/_persistence.py), [metadata-bearing result rewriting](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-compaction/src/maf_compaction/_tool_result.py), and [media estimation](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-compaction/src/maf_compaction/_media.py).
- [Cachebench factories and ConfiguredClient](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-cachebench/src/maf_cachebench/_providers.py), [matrix runtime lifetime](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-cachebench/src/maf_cachebench/_cli.py), [error/retry handling](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-cachebench/src/maf_cachebench/_runner.py), and [provider extras](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-cachebench/pyproject.toml).
- [Local MCP client in sample 21](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/samples/21_shell_storage/agent.py), [our independent MCP server](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/packages/maf-sandbox-mcp/src/maf_sandbox_mcp/server.py), and [ported Markdown helper](https://github.com/sokolaidev/maf-extensions/blob/375a60ad7ad3f4c4379b4efe70cb02f266f276ff/scripts/check_md_code_blocks.py).

A package or API described below as unused means no declared dependency or production/sample import/construction was identified in this snapshot. A similarly named transitive SDK, research note, or test does not establish use of its Agent Framework adapter. Downstream applications can use additional integrations and need their own assessment.

## Item-by-item assessment

Every row is complete as an assessment. **P1** belongs in the adoption validation/change set, **P2** is optional follow-up, and **No change** means there is no current repository consumer or the existing implementation already covers it. Proposed implementation remains pending.

| Item / upstream source | Change | Impact on this repository | Disposition / action |
|---|---|---|---|
| [#9828](https://github.com/microsoft/agent-framework/pull/9828) | Local MCP approval defaults and exact-name matching | Sample 21 uses a local MCP client with no per-tool policy; its current fallback remains unchanged. Explicit defaults also cover subsequently discovered tools and prompts; a name supplied as a string is now matched as one name. | P1: state sample policy explicitly; test mixed lists, default, overlap, and reload. Hosted helpers remain a separate policy surface. |
| [#9589](https://github.com/microsoft/agent-framework/pull/9589) | Shared telemetry resource marker | maf-sandbox-otel accepts host telemetry providers; it does not call upstream setup_observability. Upstream-created resources gain the string microsoft.agent-framework=True, with explicit overrides preserved. | P1: verify shared-resource integration and override precedence; do not assume our recorder adds the marker to host-created resources. |
| [#9384](https://github.com/microsoft/agent-framework/pull/9384) | Agent-triggered compaction and skill unloading | Directly adjacent to maf-compaction. The wrapper triggers an existing strategy, requires explicit tool registration and client-managed history, and preserves mixed tool batches. Skill unloading relies on successful result provenance. Existing strategies are not replaced. | P2: optional experiment only; test store=False, continuation rejection, occurrence identity, concurrent sessions, protected groups, and persisted replay. Propagate requires_client_managed_history through any new wrappers/closures. |
| [#9669](https://github.com/microsoft/agent-framework/pull/9669) | Await async switch-case predicates | No WorkflowBuilder/switch-case production consumer found; direct Agent and our compaction flow are unaffected. | No repository migration; add async-branch coverage if workflow support is introduced. |
| [#9405](https://github.com/microsoft/agent-framework/pull/9405) | Load Agent Hooks from host declarations | No current Agent Hooks dependency or loader. It governs host middleware declarations, not our sandbox policy enforcement or host-tool transport. | P2: evaluate as a separate integration; do not equate declaration validation with backend enforcement. |
| [#9654](https://github.com/microsoft/agent-framework/pull/9654) | Prefix FileAccessProvider tool names | Our FILE_STORE_WRITE_TOOLS and file_store_provenance_middleware observe default names; also_observes can register prefixed names. ScopedFileStores uses unprefixed providers. Outputs helpers also carry stale rationale about fixed upstream names. | P1: cover prefix-to-store provenance, default names, custom instructions and instance approval rules. Keep each store's record separate. Do not remove bounded/read-only outputs helpers solely because names can now differ. |
| [#9748](https://github.com/microsoft/agent-framework/pull/9748) | Optional chat-client contexts and ownership-aware cleanup | Cachebench builds OpenAI, Foundry, Mistral and Ollama clients. ConfiguredClient forwards ordinary attributes but not async-context special methods. Factories create caller-supplied credentials and an Ollama SDK client; upstream deliberately leaves those open. Agent contexts now close owned underlying connections. | P1: give ProviderRuntime an explicit lifetime; close created clients, supplied SDK clients and credentials at sweep end, including errors. Preserve shared client reuse across benchmark cells. Audit sample cleanup and double-close/cancellation. |
| [#9699](https://github.com/microsoft/agent-framework/pull/9699) | Foundry HTTP timeout option | _providers._build_foundry constructs FoundryChatClient without a transport timeout. ProviderCaller already bounds the whole request with asyncio.wait_for. | P2: expose an optional transport timeout if useful; retain the total request deadline and test cleanup of timeout-derived clients. |
| [#8920](https://github.com/microsoft/agent-framework/pull/8920) | NVIDIA NIM embeddings and OpenAI request-options hook | No framework embedding client, vector-store consumer, or NVIDIA framework package in our production imports. Cachebench measures chat context, not embedding retrieval. | No adoption dependency; optional future benchmark requires separate scope and metrics. |
| [#9461](https://github.com/microsoft/agent-framework/pull/9461) | Redis scope isolation and incompatible index schema | No agent-framework-redis dependency or RedisContextProvider integration. The upstream migration is real: old index plus context hashes require replacement and re-ingestion; overwrite_index does not migrate them. | No local migration. Document the requirement if a downstream host combines our suite with Redis; assess that host's retained data separately. |
| [#9824](https://github.com/microsoft/agent-framework/pull/9824) | Python 3.11 supported baseline; 3.10 classifiers removed | All 20 local packages already require Python >=3.12,<3.15. Upstream 1.24 still permits installation on 3.10 in Requires-Python; removal from installation metadata is deferred. | No local Python-floor change; retain 3.12, 3.13 and 3.14 validation. |
| [#9802](https://github.com/microsoft/agent-framework/pull/9802) | Bound Monty attachment capture | We do not consume agent-framework-monty. Our own sandbox output limits do not inherit this implementation. | No code change; preserve independent backend/output quota tests. |
| [#9795](https://github.com/microsoft/agent-framework/pull/9795) | Apply upstream shell output limits to stderr | maf-sandbox-shell uses this repository's shell workload and backend transport, not agent-framework-tools. | No inherited fix to claim. Keep stdout/stderr/truncation qualification in our own shell package. |
| [#9792](https://github.com/microsoft/agent-framework/pull/9792) | DevUI generated-file display and URLs | No agent-framework-devui runtime dependency. maf-sandbox-tui is a separate implementation. | No code change; inspect only if adding DevUI as a host. |
| [#9833](https://github.com/microsoft/agent-framework/pull/9833) | DevUI Zustand 5.0.16 | No upstream DevUI frontend is bundled here. | No local frontend dependency bump. |
| [#9835](https://github.com/microsoft/agent-framework/pull/9835) | DevUI ESLint 10.13.0 | No shared DevUI build pipeline. The tagged release also pins TypeScript 5.9.3 and rebuilds the shipped bundle. | No local tooling migration. |
| [#9839](https://github.com/microsoft/agent-framework/pull/9839) | NVIDIA permits Flit Core 4.x | Local packages use uv-build; NVIDIA is not installed as a framework integration. | No build-backend change. |
| [#9838](https://github.com/microsoft/agent-framework/pull/9838) | Pyrefly 1.4.0 dependency group | This repository uses Pyright and Ruff. Upstream adapts explicit provider option types. | No automatic checker switch; run our strict Pyright against the new public stubs. |
| [#9840](https://github.com/microsoft/agent-framework/pull/9840) | Pyrefly 1.4.0 update reference | Another referenced proposal for the same final upstream checker update; not a second runtime change. | No local action; reconciled against the tagged tree. |
| [#9842](https://github.com/microsoft/agent-framework/pull/9842) | Pyrefly grouped update reference | Same final 1.4.0 checker state as the other Pyrefly references. | No local action; count once as a tooling effect. |
| [#9834](https://github.com/microsoft/agent-framework/pull/9834) | Griffe 2.4.1 compatibility analysis | Our release gates do not use Griffe. Dropping attribute-value breakage reporting is upstream tooling behavior. | No local dependency change; do not treat upstream API checking as proof our private hooks remain compatible. |
| [#9837](https://github.com/microsoft/agent-framework/pull/9837) | Standalone Lab LiteLLM 1.106.2 | No agent-framework-lab or LiteLLM dependency in our declared/locked framework cohort; Lab is excluded from the release manifest. | No local upgrade. |
| [#9695](https://github.com/microsoft/agent-framework/pull/9695) | Content Understanding SDK floor 1.2.0b3 | maf-sandbox-document uses its own document processing stack, not agent-framework-azure-contentunderstanding. | No document-package SDK change. |
| [#9673](https://github.com/microsoft/agent-framework/pull/9673) | Correct built-in client selector examples | Cachebench has its own provider selector map; direct constructors in our samples do not use the upstream sample get_client factory. | Do not rename our azure/azure-responses provider choices based on this documentation correction. |
| [#9670](https://github.com/microsoft/agent-framework/pull/9670) | Correct harness file-memory setup | Our file-store registry directly constructs FileAccessProvider; it does not use create_harness_agent memory arguments. | No API migration; distinguish file access from file memory in future examples. |
| [#9700](https://github.com/microsoft/agent-framework/pull/9700) | Skills sample environment variables | No copied upstream skills sample or FOUNDRY_MODEL-based skills configuration identified. | No environment-variable rename in our samples. |
| [#9113](https://github.com/microsoft/agent-framework/pull/9113) | Foundry Toolbox tool-search sample | Our MCP server and Learn client do not construct Foundry Toolbox definitions. | No direct change; optional MCP discovery fix is separately covered by #9746. |
| [#9741](https://github.com/microsoft/agent-framework/pull/9741) | Foundry memory embedding environment variable | No Foundry memory-store definition or embedding configuration in our production source. | No local configuration change. |
| [#9446](https://github.com/microsoft/agent-framework/pull/9446) | Redis sample configuration and lifecycle guide | No Redis context provider integration. | No code change; retain retrieval-versus-history distinction if adopting later. |
| [#9579](https://github.com/microsoft/agent-framework/pull/9579) | Repair ChatKit sample startup and weather widgets | No ChatKit adapter. Our tools already use Content/function-result shapes rather than the removed FunctionResultContent sample pattern. | No local migration. |
| [#9599](https://github.com/microsoft/agent-framework/pull/9599) | Restore Semantic Kernel model arguments in migration sample | No Semantic Kernel orchestration path. Deep Agents/AutoGen adapters are separate integrations. | No local model-argument rename. |
| [#9580](https://github.com/microsoft/agent-framework/pull/9580) | Correct sample commands pointing to missing files | Upstream sample filenames are not our sample entry points. | No copied command repair found. |
| [#9641](https://github.com/microsoft/agent-framework/pull/9641) | OpenViking community entry | Catalog discovery only; no integration added to this suite. | No automatic dependency or feature adoption. |
| [#9683](https://github.com/microsoft/agent-framework/pull/9683) | Membase community entry | Catalog discovery only; no current context-provider integration. | No automatic dependency or feature adoption. |
| [#9660](https://github.com/microsoft/agent-framework/pull/9660) | BotsRated community entry | Catalog entry does not change our middleware or payments behavior. | No action. |
| [#9618](https://github.com/microsoft/agent-framework/pull/9618) | ACGS community entry | Catalog entry does not add enforcement to our tools. | No action; assess explicitly if requested later. |
| [#9733](https://github.com/microsoft/agent-framework/pull/9733) | Codespaces development setup guidance | Upstream contributor environment only. | No local runtime or workflow change. |
| [#9637](https://github.com/microsoft/agent-framework/pull/9637) | Python coding standards and review guidance | Upstream repository instructions; our AGENTS.md and CONTRIBUTING.md remain authoritative here. | No policy replacement. |
| [#9706](https://github.com/microsoft/agent-framework/pull/9706) | Repository helper fixes | The Markdown code-block checker originated upstream. Our check_md_code_blocks.py already uses glob.escape in literal/no-glob mode. The upstream legacy dependency-selector and trend-table helpers are not the same local tools. | No missing Markdown port found; keep our existing tests. Do not copy unrelated helper changes. |
| [#9668](https://github.com/microsoft/agent-framework/pull/9668) | Getting-started constructor/environment guidance | Our samples read their own environment variables and pass explicit values. Upstream placeholder instructions do not change constructor behavior. | No configuration migration; audit examples when modifying lifecycles. |
| [#9801](https://github.com/microsoft/agent-framework/pull/9801) | Blocking dependency-bound checks for release changes | Useful upstream release assurance, but it does not cover our packages or private APIs. We already have wheel, declared-core and sample-floor gates. | P1: run our own bounds/clean-wheel checks; optional CI comparison later. |
| [#9821](https://github.com/microsoft/agent-framework/pull/9821) | Release manifests and Azure DevOps publishing | Upstream packages are independently versioned and selected in a reviewed manifest. A tag alone is not proof every wheel published. | Assessment checked the five used candidate versions on PyPI; retain per-package publication checks during adoption. |
| [#9574](https://github.com/microsoft/agent-framework/pull/9574) | Typed in-memory vector filters | No upstream in-memory vector collection consumer. FileStoreProvenance is not this vector store. | No local migration; benefit applies only to hosts using these collections. |
| [#9825](https://github.com/microsoft/agent-framework/pull/9825) | Reject checkpoint metaclasses and aliases | Our SessionPersistence saves detached JSON session dictionaries under a host lease; it does not invoke restricted pickle checkpoint reconstruction. | No direct storage-format migration. Qualify separately if a host uses framework workflow checkpoint storage. |
| [#9720](https://github.com/microsoft/agent-framework/pull/9720) | Keep workflow-agent streaming usage | Cachebench measures direct agents/clients, not workflow.as_agent. Current direct usage accounting is unchanged by this fix. | No benchmark reclassification; test usage if workflow benchmark routes are added. |
| [#8729](https://github.com/microsoft/agent-framework/pull/8729) | Filter non-assistant workflow output | No WorkflowAgent or Foundry-hosting output path in production source. | No direct response filtering change; covers both Core and hosting release references. |
| [#9644](https://github.com/microsoft/agent-framework/pull/9644) | Finish checkpoint saves after sibling failure | Our session lease and save-on-success adapter is not the functional-workflow checkpoint scheduler. | No direct patch; never infer exactly-once sandbox effects from this workflow fix. |
| [#9261](https://github.com/microsoft/agent-framework/pull/9261) | Resolve checkpoint timestamp ties through lineage | Applies to Core, Cosmos and Foundry hosting. Our session store uses explicit IDs and host storage callbacks, not get_latest checkpoint selection. | No local migration; one cross-package change covers all repeated release references. |
| [#9260](https://github.com/microsoft/agent-framework/pull/9260) | Reject oversized MessagePack records before append | No production FileHistoryProvider construction found. Users can supply framework history providers alongside maf-compaction. Private run-persistence gates remain unchanged in the tag diff. | P1: test oversized-media save refusal and unchanged existing history in host integration; our JSON SessionPersistence limits remain the host's responsibility. |
| [#9608](https://github.com/microsoft/agent-framework/pull/9608) | File/directory collision checks and bounded paths | Direct impact: outputs sinks, ScopedFileStores, MCP server and samples use InMemoryAgentFileStore. Ancestor collisions, directory writes and excessive path depth/length are now refused; directory deletion remains file-only. | P1: test sink partial failure, retained prior content and provenance on refusal; avoid assuming every slash-separated output path is writable. |
| [#9445](https://github.com/microsoft/agent-framework/pull/9445) | Scope FIDES quarantine clients to owning config | Sandbox tools integrate with label tracking, but no production SecureAgentConfig/quarantined_llm wiring is supplied here. Hosts using multiple configs receive stronger isolation; standalone global fallback remains. | P1: integration regression with two configs and explicit None; retain result-contract, provenance and outbound-confidentiality checks. |
| [#9746](https://github.com/microsoft/agent-framework/pull/9746) | Allow optional-task MCP tools as plain calls | Sample 21's Learn client can discover tools previously filtered out. maf-sandbox-mcp is a server and does not inherit the client discovery filter. | P1: cover optional tools admitted and required-task tools rejected; do not claim MCP Tasks support. |
| [#9314](https://github.com/microsoft/agent-framework/pull/9314) | Reject non-positive MCP request timeouts | Sample 21 uses 60 seconds and remains valid. Cachebench's zero-as-disabled timeout belongs to another API and must not be bulk-rewritten. | P1: keep positive MCP timeout; validate None versus zero at any future MCP config boundary. |
| [#9614](https://github.com/microsoft/agent-framework/pull/9614) | Move workflow type alias to module scope | Griffe/API-analysis fix; no local EdgeGroup alias dependency. | No action. |
| [#9436](https://github.com/microsoft/agent-framework/pull/9436) | Preserve citations and distinguish coordinate spaces | Compaction copies Content and refuses default rewriting of metadata-bearing text. New annotated_regions_coordinate_space must survive sessions/replay; source coordinates must not become generated-text offsets. More preserved metadata can reduce text eligible for shortening. | P1: citation round-trip and no-unsafe-rewrite regressions; compare streamed/non-streamed provider payloads, including custom rewrite hooks. |
| [#9718](https://github.com/microsoft/agent-framework/pull/9718) | Anthropic server-tool continuation and replay | No direct Agent Framework Anthropic route. A transitive anthropic SDK via Deep Agents is not this adapter. | No direct adoption change; retain hosted/tool-content compatibility tests in compaction. |
| [#8980](https://github.com/microsoft/agent-framework/pull/8980) | Anthropic parallel-tool option without tool_choice | No framework Anthropic client constructor in our production code. | No action; do not attribute this fix to LangChain's separate adapter. |
| [#8875](https://github.com/microsoft/agent-framework/pull/8875) | Refuse unrestorable Cosmos checkpoints before saving | No Cosmos checkpoint storage adapter here. | No local migration; host-specific allowlist validation if introduced. |
| [#9302](https://github.com/microsoft/agent-framework/pull/9302) | Claude preserves declared tool JSON schemas | Our MCP server declares schemas through MCP 2; it does not use ClaudeAgent. | No direct fix inherited by our server; qualify separately if a Claude host is added. |
| [#9606](https://github.com/microsoft/agent-framework/pull/9606) | Declarative workflows retain function approval requirements | Our FunctionTool glue sets approvals directly; no declarative action loader is used. | No implementation change; downstream declarative hosts benefit from the upstream fix. |
| [#9507](https://github.com/microsoft/agent-framework/pull/9507) | DevUI unsupported items return 400 instead of 404 | No DevUI service endpoint in this repository. | No local error-contract change. |
| [#9499](https://github.com/microsoft/agent-framework/pull/9499) | Merge hosted MCP allow/approval lists | OpenAI/Foundry providers are installed, but no current get_mcp_tool call found. This is separate from our local MCP server and tool approval middleware. | P1: provider compatibility test before supporting hosted MCP policies; preserve both lists and test overlaps. |
| [#9705](https://github.com/microsoft/agent-framework/pull/9705) | Wrap undecodable base64 embedding errors | No current framework embedding request path; upstream OpenAI/Foundry errors gain a consistent framework wrapper. | No runtime change needed here; embeddings remain out of the chat benchmark scope. |
| [#9351](https://github.com/microsoft/agent-framework/pull/9351) | Preserve generated image bytes in Foundry hosting | No Foundry hosting server. Our media/file-store outputs are a separate path. | No local image transport change; do not claim our output bridge is qualified by this fix. |
| [#9736](https://github.com/microsoft/agent-framework/pull/9736) | Gemini non-streaming model identity | Cachebench has no Agent Framework Gemini provider route. A transitive Google SDK does not create one. | No immediate metrics change. |
| [#9649](https://github.com/microsoft/agent-framework/pull/9649) | Compose GitHub Copilot session hooks | No agent-framework-github-copilot adapter in our dependency graph. | No hook change; separate from the new generic Agent Hooks loader. |
| [#9768](https://github.com/microsoft/agent-framework/pull/9768) | Preserve Responses message boundaries and IDs | High relevance to maf-compaction occurrence/group matching, session replay, Cachebench response counting and streaming. Multiple assistant messages must not collapse into one; provider message IDs are not function call IDs. | P1: replay/stream parity tests with multiple message IDs, citations, logprobs and hosted approvals; refresh benchmark baselines only after validation. |
| [#9616](https://github.com/microsoft/agent-framework/pull/9616) | Retain Responses SSE error metadata | Cachebench classifies provider errors and uses string fallback for options/retries. Structured code/message/param now survives in the wrapped cause. | P1: add streamed error classification tests through the actual provider wrapper. Prefer structured metadata where available while retaining old-shape fallbacks. |
| [#9744](https://github.com/microsoft/agent-framework/pull/9744) | Capture final code-interpreter outputs | Core/OpenAI content can now contain complete hosted logs/images that were previously absent. Our CodeAct sandbox is a different execution path, but generic media compaction can encounter these results. | P1: test completed hosted outputs, media estimates and replay; no claim that our CodeAct execution changed. |
| [#9512](https://github.com/microsoft/agent-framework/pull/9512) | Reject misaligned base64 embeddings descriptively | No framework embedding consumer; error names the input and invalid byte alignment. | No code change. |
| [#9502](https://github.com/microsoft/agent-framework/pull/9502) | Clamp Magentic round count below one | No agent-framework-orchestrations/Magentic consumer. | No action. |
| [#9717](https://github.com/microsoft/agent-framework/pull/9717) | Clarify HandoffBuilder output selectors | Documentation only; no HandoffBuilder consumer. | No action. |
| [#9672](https://github.com/microsoft/agent-framework/pull/9672) | Correct Ollama embedding truncate default | Our Ollama provider uses chat; the corrected embedding default does not change runtime code. | No benchmark option change. |
| [#9437](https://github.com/microsoft/agent-framework/pull/9437) | Make response-to-updates examples self-contained | Documentation-only fix to examples; no serialization implementation change. | No code migration; keep our real round-trip coverage. |

The following rows cover the unlinked release entries and preparation details. Their source is the tagged changelog and [release preparation #9847](https://github.com/microsoft/agent-framework/pull/9847).

| Item | Change | Impact | Disposition / action |
|---|---|---|---|
| M01 | Oracle driver floor >=3.0 | No agent-framework-oracle consumer. | No change. |
| M02 | Package-local test dependencies, SDK-compatible test transports/assertions, and virtualenv exclusion | Upstream validation improvements, including NVIDIA HTTPX, Pydantic and Foundry compatibility; no new runtime contract here. | No copied test dependency changes; our own gates remain required. |
| M03 | Claude Agent SDK >=0.2.140 for MCP 2 custom tools | Our server uses mcp==2.2.0 directly; no Claude Agent SDK adapter. | No new Claude dependency; preserve the existing MCP pin unless separate evidence requires movement. |
| M04 | PyMongo >=4.15 for DocumentDB/MongoDB typing | Neither framework storage adapter is used. | No local database driver bump. |
| M05 | Root exact Core pin and standard-install cohort | We declare bare Core, not the umbrella package/core[all]. Eleven direct consumers exclude 1.24, and provider versions have independent numbering. | P1: update the coordinated direct Core band and selected providers; do not add every umbrella extra. |
| M06 | Core >=1.24 floors in lifecycle/citation/checkpoint consumers | OpenAI, Foundry, Mistral and Ollama in our environment require the new Core for their new releases. | P1: upgrade those five distributions together. Keeping Core <1.24 prevents selecting the new provider cohort. |
| M07 | OpenAI >=1.18 floors in Foundry/Foundry Local/NVIDIA | Foundry 1.16 is a current optional Cachebench route; the other two providers are unused. | P1: align OpenAI and Foundry floors/lock together. |
| M08 | Exact shared-hosting adapter pins | No agent-framework-hosting adapters declared or locked. | No local change. |
| M09 | MCP HTTP span attributes use HTTPX 2 | MCP 2.2 is already present, together with httpx2; legacy httpx also remains for independent consumers. The change fixes transport telemetry, not egress enforcement. | P1: a local MCP mock-transport span regression; do not remove httpx from other legitimate consumers. |
| M10 | Foundry-hosting retry dependency | The new retry requirement serves Toolbox subscription recovery; no hosting dependency here. | No direct dependency change. |
| M11 | Release preparation and public enum-value API protection | The release commit also updates compatibility tooling, formatting, manifest checks, strict dependency-floor validation, and an older-SDK optional ComputerTool factory path. These complete release preparation beyond the linked changelog entries. | No private-API compatibility inference from upstream checks; no local ComputerTool use. Record publication per used package. |

## Verification completed

All executions below used a separate archive of main and an isolated Python 3.13.12 environment. No product source, declared requirement, lockfile or generated release file was changed. Candidate runs put the five published candidate distributions ahead of the locked environment on PYTHONPATH; all other dependencies stayed at the locked baseline. This deliberately bypasses our current <1.24 metadata ceilings to test behavior. It is **not** a supported installation or a clean resolver qualification.

| Check | Result | What it establishes |
|---|---|---|
| Locked baseline: all maf-compaction and maf-cachebench tests, plus sandbox test_file_provenance.py and test_maf_glue.py | 3,120 passed; 5 warnings; 186.53 s | Baseline for the same selected tests |
| Candidate cohort: identical selection | 3,120 passed; 5 warnings; 113.96 s | No regression found by this selection on Python 3.13.12 |
| Strict Pyright for maf-compaction | 0 errors, 0 warnings | Candidate directory verified in import search paths; private compaction integration type-checks locally |
| Actual FileAccessProvider write through our middleware, default name | File written; label UNTRUSTED | Existing default wiring records the write |
| Actual prefixed write, no also_observes registration | File written; label TRUSTED under a configured TRUSTED floor | Reproduces the new-option integration hazard; default-name divergence test alone does not catch it |
| Same prefixed write with explicit registration | File written; label UNTRUSTED | Existing explicit-name workaround works |
| ConfiguredClient wrapping an async-context-capable client | Inner supports async context; wrapper does not; explicit close forwards | Special methods need explicit lifetime handling; ordinary attribute forwarding is insufficient |
| Source-coordinate citation in a function-result text item | Coordinate marker survives Content dictionary round trip; default shortening is refused | Existing metadata protection handles this probed shape |
| In-memory ancestor file collision | NotADirectoryError | Candidate enforces the upstream collision rule |
| Upstream _sessions.py comparison | Only MessagePack append/record validation changed | Referenced private run-persistence gates did not change in this release |
| Inventory reconciliation | 74/74 linked PRs, 10/10 unlinked bullets, plus preparation row | Every one of the 46 changelog bullets maps to an assessed item |

The initial sandboxed test attempt could not fetch tiktoken's encoding data; the parallel attempt was stopped. The completed baseline and candidate runs used the permitted network-capable environment. Neither the setup failure nor the stopped attempt is counted as a compatibility result. Faster candidate elapsed time is not a performance claim: caches and process conditions differed.

Retained local evidence is under `.artifacts/maf-1.24-assessment/`: `baseline-focused.log`, `candidate-focused.log`, `candidate-pyright-compaction.log`, `probes.py`, `probes.json`, the tagged source archives, the main archive, and `release-item-ledger.csv`. The raw logs include local execution paths and are not intended for public publication unchanged.

Not verified: the full repository gate; other packages' complete suites or strict typing; Python 3.12/3.14; Linux; a newly resolved lock; all optional extras and clean built-wheel installs; hosted CI; real OpenAI/Foundry/Mistral/Ollama calls; Docker, AKS, Hyperlight, WSLC or other native backends; production stores; Redis migration; and live cost/cache/recall quality. No package release or downstream production readiness is claimed.

## Plan of action

The assessment is complete. The following implementation tasks are proposed and remain open; no issue, PR, comment or release was published by this assessment.

| Order | Work item | Concrete change / scope | Acceptance criterion | State |
|---|---|---|---|---|
| 1 | Coordinated dependency adoption | Update the eleven direct Core consumers to the qualified 1.24 band; align the root dev group and Cachebench OpenAI, Foundry, Mistral and Ollama extras with the cohort above; regenerate uv.lock; update compatibility documentation. Preserve the direct MCP 2.2 pin unless separate evidence requires movement. | A fresh solve admits every workspace package and optional provider extra; framework-ceiling/locked-framework checks agree with declarations. | Proposed |
| 2 | Prefix-safe file provenance and store failures | Add supported wiring/documentation for exact prefixed mutation names with one provenance record per store. Cover all four mutations, custom prefixes, default coexistence, prefix collisions, both middleware orders, exceptional writes and two-store isolation. Update obsolete fixed-name rationale without weakening bounded outputs helpers. Add file/directory and path-limit failure coverage for file sinks/registry/MCP usage. | The reproduced TRUSTED result for model-written prefixed content becomes impossible under the supported wiring; failed/partial writes remain conservatively labeled; previous bytes survive refusals. | Proposed; existing also_observes workaround verified |
| 3 | Provider ownership and cleanup | Define ProviderRuntime ownership of framework clients, externally supplied SDK clients and credentials it creates. Add an async lifetime or explicit close path; handle ConfiguredClient special methods deliberately. Cover matrix/live/summary/recall/advisor entry points and samples; close once after the shared-client sweep, not per cell. | Owned resources close on success, errors and cancellation; caller-owned resources remain usable; subsequent cells do not reuse a closed client. | Proposed |
| 4 | Focused compatibility regressions | Pin multiple Responses message identities and stream/non-stream parity; citation coordinate preservation; final code-interpreter media; structured SSE errors; FIDES per-config/None isolation; MessagePack refusal; optional MCP task discovery; approval-list/default behavior; HTTPX2 telemetry. Include hosted search grouping introduced in the compaction change. | Tests execute the real installed adapter boundary with synthetic events/transports where possible; no orphaned calls/results, unsafe citation rewrites, trust promotion or persistence of refused turns. | Proposed; existing focused suite passed |
| 5 | Full local, wheel and hosted validation | Run uv sync and poe gate; md-blocks; sample-floors if samples change; all package wheel/metadata/clean-install checks, suite-install and declared/published-core compatibility checks. Run Python 3.12-3.14 on supported CI platforms and normal image checks for changed image inputs. | Exact PR head is green; any baseline/platform failure is separately reproduced and reported; local green is not called full CI green. | Proposed |
| 6 | Bounded live qualification | Exercise OpenAI Chat Completions and Responses, Foundry, Mistral and Ollama routes actually supported by Cachebench; include streaming, a multi-tool turn, compaction/recall, save/restore, cancellation and a representative sandbox tool. Retain model/config/source/version identities. | No message loss, missing tool output, label leakage, unexpected persistence or resource leak; usage and recall observations recorded separately from local tests. | Proposed |
| 7 | PR and maintainer-controlled release sequence | Deliver reviewed changes with an honest Conventional Commit title for the final package scope and the repository PR template. Revalidate prepared maf-sandbox/maf-compaction versions before raising dependent ranges; move both ends when necessary. Update tracked status rows if an issue is closed. | Local and hosted evidence attached; maintainer publishes required core/compaction artifacts before dependent uploads; samples range PR merges only once its dependent versions are published. No hand-edited versions/changelogs/manifests. | Proposed |
| 8 | Optional features, separately | Benchmark agent-triggered anchored compaction and skill unloading; consider Foundry transport timeouts or Agent Hooks declaration integration only with a concrete host need. | Client-history restriction survives wrappers; triggers occur once per occurrence; protected groups, recall validity and per-session state survive replay; measured quality/cost justifies adoption. | Deferred |

Do not introduce a generic compaction wrapper into `SessionCompactionRegistry` without preserving its session-specific state/snapshot contract. Upstream's trigger wrapper does not make our underlying stateful strategies safe to share across unrelated conversations, and unloading skills is not equivalent to persisting validated tool-result summaries.

Do not apply the Redis destructive migration to any host merely because this suite upgrades. It is relevant only to hosts actually using the affected Redis context index, and their data migration needs a separate retained-data plan.

## Release-bullet crosswalk

All entries below are assessed. Numbers refer to the 46 bullets in order in the tagged 1.24.0 changelog, not to implementation progress.

| Changelog bullet | Completed ledger items |
|---|---|
| 1 | #9828 |
| 2 | #9589 |
| 3 | #9384 |
| 4 | #9669 |
| 5 | #9405, #9654 |
| 6 | #9748 |
| 7 | #9699 |
| 8 | #8920 |
| 9 | M01 |
| 10 | M02 |
| 11 | M03 |
| 12 | M04 |
| 13 | #9461 |
| 14 | #9824 |
| 15 | #9802 |
| 16 | #9795 |
| 17 | #9792 |
| 18 | #9833, #9835 |
| 19 | #9839 |
| 20 | #9838, #9840, #9842, #9834, #9837 |
| 21 | #9695 |
| 22 | M05 |
| 23 | M06 |
| 24 | M07 |
| 25 | M08 |
| 26 | #9673, #9748, #9384, #9670, #9700, #9113, #9741, #9446, #9579, #9599, #9580, #9641, #9683, #9660, #9618 |
| 27 | #9733, #9637, #9706, #9668, #9801, #9821 |
| 28 | M09 |
| 29 | M10 |
| 30 | #9574 |
| 31 | #9825 |
| 32 | #9720, #8729, #9644, #9261, #9260, #9608, #9445 |
| 33 | #9746, #9314, #9614 |
| 34 | #9436 |
| 35 | #9718, #8980 |
| 36 | #8875, #9261 |
| 37 | #9302 |
| 38 | #9606 |
| 39 | #9507 |
| 40 | #9499, #9705 |
| 41 | #9351, #9261, #8729 |
| 42 | #9736 |
| 43 | #9649 |
| 44 | #9768, #9616, #9744, #9512 |
| 45 | #9502, #9717 |
| 46 | #9672, #9437 |


## MAF 1.25 assessment and adoption plan

> Completed source assessment on 2026-11-10 against the pinned revisions below. Adoption is proposed, not implemented. The earlier 1.24 assessment above remains historical evidence. This section extends the existing research record as required by this directory's AGENTS.md.

### 1.25 decision

Proceed with a coordinated adoption of **Core 1.25.0, OpenAI 1.18.1, Foundry 1.16.1, Mistral 1.0.0b261106 and Ollama 1.0.0b261106**, after the qualification work below. The implementation tracked by [#2641](https://github.com/sokolaidev/maf-extensions/issues/2641) moves the measured band to `>=1.25.0,<1.26`; its [qualification and release sequence](../../release-compatibility.md#maf-125-adoption) supersede the assessment-only state below. Do not widen to `<2` based on upstream metadata.

The selected 3,496-test suite passed on both the locked 1.24 cohort and the published 1.25 cohort without changing product code. This supports proceeding, but does not qualify every shipped package or a normal consumer installation. The highest-priority work is FIDES policy/materialization and middleware ordering (#9875), followed by MCP discovery/concurrency (#10042/#10147/#10136), response metadata and middleware termination (#9403/#9681), and provider replay/aggregation (#9931/#9094/#9803/#9763). The private compaction and session modules are unchanged byte-for-byte between the installed Core wheels; callers around them still changed.

Seven upstream breaking-change groups have been reviewed. Python 3.10 removal needs no local interpreter migration. Sample 21 already sets `request_timeout=60`, so it does not inherit the new 30-second default. The upstream AG-UI, declarative, Foundry Local and Hyperlight/Monty migrations do not directly apply to our independently implemented adapters. No new optional provider, workflow host, memory service or provider-native shell is proposed.

### 1.25 scope, completeness and source identity

- Upstream release: [python-1.25.0](https://github.com/microsoft/agent-framework/releases/tag/python-1.25.0), published 2026-11-07, tagged commit `7bca1df23ea97b778dd5cd7a50d9db8d1ef832db`. The [tagged changelog](https://github.com/microsoft/agent-framework/blob/7bca1df23ea97b778dd5cd7a50d9db8d1ef832db/python/CHANGELOG.md) dates the version 2026-11-06.
- Baseline upstream tag: `python-1.24.0`, commit `43d767c55050480e470ebf0a89caa4141d325ebf`. The [tag comparison](https://github.com/microsoft/agent-framework/compare/python-1.24.0...python-1.25.0) contains 118 commits, retrieved across both API pages. The aggregate changed-file response stops at 300 files, so it was not used as a complete file inventory; per-PR inventories were retrieved separately, including all 302 files for #9948.
- Release inventory: **46 bullets, 77 distinct referenced PRs, three unlinked dependency bullets**. The PR ledger below covers all 77 separately. Release preparation #10151 and the source-present documentation change associated with #9617 are assessed separately, and another 43 tag-range PRs are explicitly excluded by scope. This reconciles **122 distinct PR references** across the notes and tag history, plus M1-M3. Assessed does not mean adopted or runtime-tested.
- Four referenced Dependabot PRs (#10115, #10140, #10129, #10128) are closed unmerged; their updates were incorporated by [release preparation #10151](https://github.com/microsoft/agent-framework/pull/10151). They are not four additional merged commits.
- Release manifest: [44 independently versioned distributions](https://github.com/microsoft/agent-framework/blob/7bca1df23ea97b778dd5cd7a50d9db8d1ef832db/python/.github/release-python-1.25.0.json). Standalone Lab is excluded. Publication and installation were checked for the five used distributions, not all 44.
- Repository baseline: [main at ea3c22d4](https://github.com/sokolaidev/maf-extensions/tree/ea3c22d4a37a8cda56f609d5d57463c609bd9413), verified against GitHub during this assessment. Work used an isolated detached worktree; the older active feature checkout was preserved.
- Published artifacts: [Core 1.25.0](https://pypi.org/project/agent-framework-core/1.25.0/), [OpenAI 1.18.1](https://pypi.org/project/agent-framework-openai/1.18.1/), [Foundry 1.16.1](https://pypi.org/project/agent-framework-foundry/1.16.1/), [Mistral 1.0.0b261106](https://pypi.org/project/agent-framework-mistral/1.0.0b261106/) and [Ollama 1.0.0b261106](https://pypi.org/project/agent-framework-ollama/1.0.0b261106/). Installed wheel metadata and actual import location were inspected.

Context7 was consulted for MCP documentation, but its returned constructor example still showed the old unlimited default and omitted discovery-limit arguments. Tagged source and installed-wheel signatures determine this assessment, not that unversioned excerpt.

### 1.25 dependency and consumer map

This table records the pre-adoption baseline and proposed changes. The implementation record above describes the resulting declarations.

| Surface | Verified current main / lock | Proposed adoption |
|---|---|---|
| Twelve direct Core consumers | All declare `>=1.24.0,<1.25`; lock is 1.24.0 | Qualify and move all twelve to `>=1.25.0,<1.26` together |
| OpenAI adapter, root development group and Cachebench extra | `>=1.18.0,<2`; locked 1.18.0 | Floor at 1.18.1 and lock it; its published metadata requires Core >=1.25 |
| Foundry adapter, same locations | `>=1.16.0,<2`; locked 1.16.0 | Floor at 1.16.1 to guarantee its replay fix; published Core floor remains 1.24 and OpenAI floor remains 1.18.0 |
| Mistral adapter, same locations | `>=1.0.0b261030,<2`; locked that beta | Qualify/lock 1.0.0b261106; no Mistral-specific runtime fix in the 1.25 notes. Raising this provider floor is a cohort policy choice, not a new Core API requirement |
| Ollama adapter, same locations | `>=1.0.0b261030,<2`; locked that beta | Floor/lock at 1.0.0b261106 for streaming finish-reason fix |
| SDK dependencies | OpenAI 3.29.0, azure-ai-projects 2.7.0, Ollama 0.5.3 in lock | All are admitted by the candidate provider requirements. Foundry widens projects to <2.10; Ollama's provider still requires >=0.5.3,<0.5.4. Avoid unrelated SDK upgrades |
| Python | All 21 packages require `>=3.12,<3.15` | Preserve; upstream's >=3.11 baseline and Redis 3.15 eligibility do not change our promise |
| MCP | Direct `mcp==2.2.0` | Preserve; candidate Core's optional MCP requirement admits it. No second MCP major upgrade |
| Nine remaining packages | Receive framework transitively; no direct Core declaration | Test through suite and install gates; do not add direct dependencies merely to equalize metadata |

The twelve direct consumers are `maf-sandbox`, `maf-compaction`, `maf-cachebench`, `maf-sandbox-bicep`, `maf-sandbox-codeact`, `maf-sandbox-terraform`, `maf-sandbox-drawio`, `maf-sandbox-shell`, `maf-sandbox-document`, `maf-sandbox-mcp`, `maf-sandbox-azure-pricing` and `maf-sandbox-antivirus`.

Source anchors for the impact judgments:

- [Sandbox framework glue](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/packages/maf-sandbox/src/maf_sandbox/maf.py): `sandbox_tool_declarations`, `argument_provenance_middleware`, `sandbox_label_tracking_middleware`, `file_store_provenance_middleware` and the public `rewritten_arguments` adapter. The argument observer shares the live context; the file observer reads its path after the downstream call, which matters when hidden arguments materialize later.
- [Recall middleware](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/packages/maf-compaction/src/maf_compaction/_toolsummary.py) and [session persistence](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/packages/maf-compaction/src/maf_compaction/_persistence.py): private compaction/run-persistence hooks, termination handling and save/restore boundaries.
- [Cachebench provider factories/wrapper](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/packages/maf-cachebench/src/maf_cachebench/_providers.py): explicit model selection, owned/borrowed client lifetime and `ConfiguredClient`. Azure, Azure Responses, Foundry, Mistral, Ollama and OpenRouter routes use the five framework distributions above; SDK presence alone does not add an Anthropic/Gemini MAF provider.
- [Sample 21](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/samples/21_shell_storage/agent.py): Microsoft Learn MCP client with an explicit 60-second timeout. [Installed-worker qualification](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/fixtures/openclaw_installed_worker_check.py) and [Bicep ownership qualification](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/fixtures/openclaw_bicep_ownership_check.py) explicitly use 240 seconds.
- [MCP HTTP service tests](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/test_mcp_workload_service.py), [framework compatibility tests](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/test_maf_124_compatibility.py), [approval contracts](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/test_framework_approval_contract.py) and [host file integrity](https://github.com/sokolaidev/maf-extensions/blob/ea3c22d4a37a8cda56f609d5d57463c609bd9413/tests/test_host_file_integrity.py) provide existing regression seams.

“No direct migration” means no declared framework adapter or production/sample API consumer was identified at this revision; it does not cover arbitrary downstream hosts. In particular, `maf-sandbox-hyperlight` is not `agent-framework-hyperlight`, `maf-sandbox-codeact` is not Monty, `maf-sandbox-mcp` is not `agent-framework-hosting-mcp`, and our shell kind is not `agent-framework-tools`.

### 1.25 PR-by-PR assessment

Every row is assessed. “Qualify” names implementation-phase evidence still required; the existing-suite results below do not automatically satisfy all proposed new assertions. P0-P6 refer to the ordered plan.

| Upstream PR | Change | Disposition | Repository impact and action |
|---|---|---|---|
| [#10136](https://github.com/microsoft/agent-framework/pull/10136) | Callable MCP discovery approval policies | Qualify | MCP consumers can choose approvals from tool/prompt metadata. No FoundryToolbox consumer exists here; do not infer approval or trust from our server annotations. P2: test valid decisions, invalid/raising callbacks, retained-tool refresh and atomic rollback. Keep the current explicit/static policy unless a host needs the callback. |
| [#9948](https://github.com/microsoft/agent-framework/pull/9948) | Python 3.10 eligibility removed | No migration | All 21 workspace packages already require >=3.12,<3.15. Python 3.11 upstream support does not expand our support promise. P1/P5: preserve our bounds and test 3.12, 3.13 and 3.14; do not adopt 3.15 because Redis now permits it. |
| [#10042](https://github.com/microsoft/agent-framework/pull/10042) | Bounded MCP discovery and finite request timeout | Qualify | Sample 21 already sets 60 seconds; ownership fixtures also set explicit timeouts. Default-created clients elsewhere now have a 30-second read timeout and 100-page/1,000-item tool and prompt catalog limits. P2: preserve explicit limits; test boundary pages/items, opaque/empty cursors, refresh rollback and borrowed-client ownership. These limits do not cap encoded response bytes. |
| [#10035](https://github.com/microsoft/agent-framework/pull/10035) | Authorization ownership for workflow executors and sessions | No direct migration | No AG-UI/hosting WorkflowAgent integration is declared. Core adds wrapped-agent ownership metadata, so generic Agent.as_tool paths still belong in compatibility coverage. P3/P5: retain ordinary agent-tool tests; do not claim this isolates our own stores or arbitrary host workflow functions. Future AG-UI use needs fresh workflow/executor/session instances per authorization scope. |
| [#9900](https://github.com/microsoft/agent-framework/pull/9900) | Workflow trace operation/name changes | No direct migration | maf-sandbox-otel observes sandbox events; no exact workflow.run/workflow.name filter was found in runtime code. Upstream workflow telemetry is a separate surface. Keep sandbox span names. Document downstream migration to gen_ai.operation.name=invoke_workflow and gen_ai.workflow.name only for hosts consuming MAF workflow spans. |
| [#8662](https://github.com/microsoft/agent-framework/pull/8662) | Fresh declarative MCP session per handler call | No direct migration | Our MCPServer and workload service do not use DefaultMCPToolHandler. Its removed async lifetime and implicit cross-call state are therefore not our server lifecycle. Do not change server pooling. Future declarative consumers must manage borrowed HTTP clients themselves; cache_max_size is only a deprecated no-op after #10151. |
| [#9322](https://github.com/microsoft/agent-framework/pull/9322) | Foundry Local SDK 2.3.1 migration | Defer | Cachebench's foundry route uses the cloud FoundryChatClient, not FoundryLocalClient; the Ollama route is also distinct. No SDK or bootstrap change. A future local route needs independent platform, model-preparation, shared-manager and cleanup qualification. |
| [#9971](https://github.com/microsoft/agent-framework/pull/9971) | Redact host exceptions in upstream CodeAct | No direct migration | maf-sandbox-codeact and maf-sandbox-hyperlight do not import the upstream Hyperlight/Monty providers. Their host-tool transport and error contracts are independent. Retain our error/redaction tests; do not add include_detailed_errors or claim upstream redaction covers this backend. |
| [#10096](https://github.com/microsoft/agent-framework/pull/10096) | Dependency updates and Monty runtime bridge | Adopt cohort | Foundry widens azure-ai-projects from <2.8 to <2.10 without raising >=2.2. The locked SDK 2.7.0 remains admitted. FastAPI/Monty changes target unused adapters. P1: lock the chosen cohort and record actual transitive versions; no automatic SDK or tooling upgrade is necessary solely because upstream widened a range. |
| [#10143](https://github.com/microsoft/agent-framework/pull/10143) | Redis permits Python 3.15 and RedisVL 0.31 | No direct migration | No framework Redis integration is installed; our Python ceiling remains <3.15. No Redis dependency or interpreter change. |
| [#10103](https://github.com/microsoft/agent-framework/pull/10103) | Clarify Purview binary classification | No direct migration | No Purview adapter; documentation does not alter our sandbox provenance or content labels. No local change. |
| [#9783](https://github.com/microsoft/agent-framework/pull/9783) | Document optional usage counters | Qualify | Cachebench distinguishes absent cache counters from measured zero. The change is documentation, not new accounting behavior. P3: preserve missing-versus-zero reporting; do not backfill unsupported counters. |
| [#9897](https://github.com/microsoft/agent-framework/pull/9897) | Document OTel logging-handler ownership | No migration | Clarifies configuration behavior; no runtime change in the PR. No logging-handler rewiring; keep our observer's ownership semantics. |
| [#10105](https://github.com/microsoft/agent-framework/pull/10105) | Remove unused URI regex and fix cycle documentation | No migration | No private URI_PATTERN import or workflow-builder consumer found. No copied compatibility shim. |
| [#9735](https://github.com/microsoft/agent-framework/pull/9735) | Correct multimodal sample settings | No migration | Upstream sample documentation only; local factories pass explicit model settings. Keep our provider-specific environment names. |
| [#9740](https://github.com/microsoft/agent-framework/pull/9740) | Use FOUNDRY_MODEL in Content Understanding examples | No migration | No Content Understanding integration; our cloud Foundry route already reads FOUNDRY_MODEL. No local rename. |
| [#9758](https://github.com/microsoft/agent-framework/pull/9758) | Correct ChatKit setup variables | No migration | No ChatKit consumer. No local change. |
| [#9781](https://github.com/microsoft/agent-framework/pull/9781) | Correct red-team sample settings | No migration | No upstream red-team sample is embedded in this repo. No local change. |
| [#9906](https://github.com/microsoft/agent-framework/pull/9906) | Repair evaluator documentation link | No migration | No Rubric evaluator code change. No local change. |
| [#10032](https://github.com/microsoft/agent-framework/pull/10032) | Add Bhived community project | No migration | Upstream community listing only. No local dependency. |
| [#10086](https://github.com/microsoft/agent-framework/pull/10086) | Add Donkey Development Kit community project | No migration | Upstream community listing only. No local dependency. |
| [#10038](https://github.com/microsoft/agent-framework/pull/10038) | Strengthen upstream API/review guidance | No migration | Contributor guidance, not a library behavior change. Keep this repository's AGENTS.md/CONTRIBUTING.md authoritative. |
| [#9937](https://github.com/microsoft/agent-framework/pull/9937) | Expand optional MCP tests | Qualify | The optional MCP route is used by sample 21 and ownership tests; this PR itself changes only upstream tests. P2: keep optional-task discovery and approval-default tests; no new task support claim. |
| [#10117](https://github.com/microsoft/agent-framework/pull/10117) | Lab pytest update | No migration | Standalone Lab tooling is outside the upstream publication batch and our dependencies. No local pytest bump. |
| [#10118](https://github.com/microsoft/agent-framework/pull/10118) | Lab mypy update | No migration | Standalone Lab tooling only. No local checker change. |
| [#10119](https://github.com/microsoft/agent-framework/pull/10119) | Upstream ty update | No migration | Repository checker pin, superseded in the release cohort by 0.0.95. No local checker change. |
| [#10120](https://github.com/microsoft/agent-framework/pull/10120) | Upstream mypy update | No migration | Repository checker pin only; this repo uses its own validation configuration. No local checker change. |
| [#9947](https://github.com/microsoft/agent-framework/pull/9947) | Fix changed-package path routing | No migration | Upstream CI selection script, not a shipped API. No port into our independent gate_tasks.py. |
| [#10115](https://github.com/microsoft/agent-framework/pull/10115) | pytest 9.2.0 update, incorporated by release prep | No migration | Original PR was closed unmerged; #10151 carries the update and aligned extras. Our pytest pin is independent. No automatic pytest upgrade; retain normal test gates. |
| [#10140](https://github.com/microsoft/agent-framework/pull/10140) | ty/Zuban updates, incorporated by release prep | No migration | Original PR was closed unmerged; final pins are delivered in #10151. No local checker change. |
| [#10129](https://github.com/microsoft/agent-framework/pull/10129) | Lab OAuthlib lock update, incorporated by release prep | No migration | Closed unmerged; #10151 incorporates the standalone lock change. It is not a suite runtime fix. No local dependency change. |
| [#10128](https://github.com/microsoft/agent-framework/pull/10128) | Telegram sample OAuthlib lock update, incorporated by release prep | No migration | Closed unmerged; #10151 incorporates the sample lock change. No Telegram hosting consumer here. No local dependency change. |
| [#10147](https://github.com/microsoft/agent-framework/pull/10147) | Request-owned MCP headers and concurrent same-identity requests | Qualify | Real HTTP ownership tests construct MAF clients against our service. Matching headers can overlap; identity transitions and recovery drain active attempts. Shared protocol state remains shared. P2: real-HTTP overlap, identity isolation, cancellation, retry metadata and no resurrection after close. Treat admission, RPC and recovery timing separately; preserve outer call deadlines. |
| [#9403](https://github.com/microsoft/agent-framework/pull/9403) | Capture run-specific tools for evaluation | Qualify | This is broader than its title: responses gain schema snapshots and a final metadata-only streaming update. Cachebench ConfiguredClient is a duck wrapper, not a BaseChatClient, so BaseChatClient-specific capture/aggregation must be tested explicitly. P3: dynamic tools, stream/final-response parity, early close, JSON round trips and wrapper behavior. Do not claim all provider tools were previously absent at execution time. |
| [#9679](https://github.com/microsoft/agent-framework/pull/9679) | Accept tuples in function results | Qualify | Our tools return rich Content sequences and compaction rewrites canonical result items; upstream now handles tuples like lists. P3: mixed tuple Content/text, nested tuple serialization and preservation of labels/annotations; preserve existing list contract. |
| [#9681](https://github.com/microsoft/agent-framework/pull/9681) | Preserve middleware termination results | Qualify | Recall middleware catches MiddlewareTermination/MiddlewareFailure to discard refused persistence. A carried result now survives agent/chat middleware if context.result is absent. P3: test carried and preassigned results, both streaming modes, and no persistence of rejected/partial recall state. |
| [#8960](https://github.com/microsoft/agent-framework/pull/8960) | Deterministic recursive tool-schema ordering | Adopt with measurement | Every sandbox FunctionTool schema and Cachebench tool request can change serialized ordering. Equivalent dictionaries are not evidence of identical prompt bytes. P3: deterministic nested object keys while list order is preserved. Start fresh cache benchmarks after upgrade; do not attribute cache changes solely to strategy quality. |
| [#9852](https://github.com/microsoft/agent-framework/pull/9852) | Normalize whole-number floats for integer schemas | Qualify | Schema-backed tools accept 3.0 as integer and normalize it when number is not also allowed; bool/fractional inputs remain distinct. This is not a blanket change to every Pydantic argument model. P3: test integer-only versus integer/number union inputs at actual tool boundaries; retain validation and post-approval authority checks. |
| [#9943](https://github.com/microsoft/agent-framework/pull/9943) | Respect chat-client serialization exclusions | Qualify | ConfiguredClient forwards ordinary methods; exclusions now apply to flattened additional_properties, including None values. P3: verify explicit exclusions and saved response/client metadata are not conflated; no claim this alone sanitizes all credentials. |
| [#9942](https://github.com/microsoft/agent-framework/pull/9942) | Deferred executor discovery returns an empty list | No direct migration | No framework Executor subclass/output_types consumer found. No adapter shim. |
| [#9998](https://github.com/microsoft/agent-framework/pull/9998) | Deserialize NoneType workflow request-info values | No direct migration | Our session persistence uses AgentSession, not workflow RequestInfoEvent rehydration. No store migration; keep general session round-trip tests. |
| [#9294](https://github.com/microsoft/agent-framework/pull/9294) | Retain uncovered sessions after memory consolidation | No direct migration | Upstream harness memory consolidation is separate from maf-compaction's coverage-based recall records. Do not replace our coverage gate or enable upstream memory consolidation. |
| [#9547](https://github.com/microsoft/agent-framework/pull/9547) | Quarantine unreadable harness memory state | No direct migration | Our compaction persistence does not use the harness memory state file. No automatic deletion/quarantine of host stores; preserve our failure behavior. |
| [#9387](https://github.com/microsoft/agent-framework/pull/9387) | Retain completed background-task results | No direct migration | No BackgroundAgentsProvider consumer identified; sandbox process cleanup has separate ownership. No new task retention or cleanup policy. |
| [#9875](https://github.com/microsoft/agent-framework/pull/9875) | Fail-closed FIDES policy and execution authority | Adoption gate | Directly intersects sandbox declarations, public rewritten_arguments, file provenance, hidden values and middleware ordering. Materialization moves to terminal execution; invalid metadata and post-approval argument/policy/session changes now refuse. P0: run real-middleware labels and every kind; add blocked-validator, mutation/retry, both-order provenance, USER_IDENTITY and surrogate persistence regressions. Keep policy validation before hidden-value execution. |
| [#9094](https://github.com/microsoft/agent-framework/pull/9094) | Preserve AG-UI citations through aggregation | Qualify shared Core path | AG-UI is unused, but Core now delegates agent finalization to BaseChatClient and function-loop finalization to the client hook. Compaction and Cachebench reconstruct streams. P3: preserve message identity, annotation ownership, replacement updates, tool-loop aggregation and wrappers; do not add AG-UI just to test our used providers. |
| [#9770](https://github.com/microsoft/agent-framework/pull/9770) | Emit one replayed AG-UI tool-start event | No direct migration | No AG-UI endpoint or event converter consumer. No event handling change. |
| [#9859](https://github.com/microsoft/agent-framework/pull/9859) | Include Anthropic cache tokens in input usage | No direct migration | The Anthropic SDK is transitive; agent-framework-anthropic is not one of our provider adapters. OpenRouter uses the OpenAI adapter. Do not alter Cachebench counters for all providers based on this fix. |
| [#9898](https://github.com/microsoft/agent-framework/pull/9898) | Stream Anthropic citation metadata | No direct migration | No Anthropic MAF adapter; common annotation preservation remains covered under #9094. No provider added. |
| [#9972](https://github.com/microsoft/agent-framework/pull/9972) | Replay Anthropic hosted MCP results | No direct migration | Neither that adapter nor its hosted MCP replay route is used. No transcript migration. |
| [#10106](https://github.com/microsoft/agent-framework/pull/10106) | Replay Anthropic redacted reasoning blocks | No direct migration | No Anthropic MAF tool-loop adapter; do not decode opaque provider data. No new replay route. |
| [#9996](https://github.com/microsoft/agent-framework/pull/9996) | Prefer injected Cosmos vector clients | No direct migration | No framework Cosmos vector-store consumer. No storage-client or credential changes. |
| [#9982](https://github.com/microsoft/agent-framework/pull/9982) | Prefer injected MongoDB clients | No direct migration | No framework MongoDB vector-store consumer. No storage-client or credential changes. |
| [#9985](https://github.com/microsoft/agent-framework/pull/9985) | Prefer injected Oracle clients | No direct migration | No framework Oracle vector-store consumer. No storage-client or credential changes. |
| [#9983](https://github.com/microsoft/agent-framework/pull/9983) | Prefer injected Postgres clients | No direct migration | No framework Postgres adapter; our persisted sessions use different stores. No storage-client or credential changes. |
| [#9993](https://github.com/microsoft/agent-framework/pull/9993) | Prefer injected Qdrant clients | No direct migration | No framework Qdrant vector-store consumer. No storage-client or credential changes. |
| [#9977](https://github.com/microsoft/agent-framework/pull/9977) | Prefer injected Redis history client | No direct migration | No framework Redis history provider; do not apply old Redis migrations to this repo by association. No storage-client or credential changes. |
| [#9796](https://github.com/microsoft/agent-framework/pull/9796) | Preserve parallel Bedrock tool results | No direct migration | No MAF Bedrock provider in Cachebench. No provider added; keep generic multi-result history checks. |
| [#9967](https://github.com/microsoft/agent-framework/pull/9967) | Prefer explicit Bedrock client/session region | No direct migration | No Bedrock provider construction. No environment changes. |
| [#9984](https://github.com/microsoft/agent-framework/pull/9984) | Skip Claude settings for an injected SDK client | No direct migration | No framework Claude adapter. No provider or SDK added. |
| [#9969](https://github.com/microsoft/agent-framework/pull/9969) | Skip Gemini settings for an injected SDK client | No direct migration | google-genai is transitive; agent-framework-gemini is not used. No change to cloud settings. |
| [#10005](https://github.com/microsoft/agent-framework/pull/10005) | Forward declarative extra model options normally | No direct migration | No declarative loader; ConfiguredClient has its own tested option-merging behavior. Do not copy declarative option wiring. |
| [#9712](https://github.com/microsoft/agent-framework/pull/9712) | Allow IPv6 loopback Host in DevUI | No direct migration | Our MCP HTTP service is not upstream DevUI. No host-header policy changes. |
| [#9931](https://github.com/microsoft/agent-framework/pull/9931) | Attach Foundry reasoning replay data to its owner | Qualify | Cachebench uses FoundryChatClient; multi-message responses can carry reasoning on a later message. Summary and session replay must keep that association. P3: later/trailing reasoning, no duplication, stream/nonstream parity, save/restore and outgoing serialized payload checks. |
| [#9789](https://github.com/microsoft/agent-framework/pull/9789) | Normalize nested SDK models in Foundry hosted tools | Qualify optional route | Foundry adapter is used; current factories do not themselves configure hosted tools. Rich payloads may come from host use. P3: synthetic nested-model serialization and refusal of malformed tool payloads; do not enable hosted tools. |
| [#9981](https://github.com/microsoft/agent-framework/pull/9981) | Honor FOUNDRY_MODEL in FoundryEvals | No direct migration | Our foundry factory already reads FOUNDRY_MODEL; no FoundryEvals consumer. No local environment rename or evaluator dependency. |
| [#9991](https://github.com/microsoft/agent-framework/pull/9991) | Allow injected Foundry embedding clients without endpoint | No direct migration | Cachebench constructs chat clients, not Foundry embedding clients. No endpoint/ownership rewrite. |
| [#9745](https://github.com/microsoft/agent-framework/pull/9745) | Preserve hosted-response request metadata | No direct migration | Foundry hosting is distinct from FoundryChatClient and is not installed. No host transport change. |
| [#10090](https://github.com/microsoft/agent-framework/pull/10090) | Reject unsupported Copilot function middleware | No direct migration | No agent-framework-github-copilot consumer; our sandbox FunctionMiddleware is used with standard Core agents. Do not advertise Copilot compatibility. |
| [#10088](https://github.com/microsoft/agent-framework/pull/10088) | Sum Copilot usage over model calls | No direct migration | No Copilot adapter; Cachebench's own usage aggregation remains provider-specific. No global accounting change. |
| [#10149](https://github.com/microsoft/agent-framework/pull/10149) | Narrow unscoped URL session approvals | No direct migration | No Copilot permission handler. Our egress allowlist and destination policy remain independent. No URL approval policy weakening or new domain inference. |
| [#10036](https://github.com/microsoft/agent-framework/pull/10036) | Validate arguments for hosted MCP agent tools | No direct migration | maf-sandbox-mcp uses MCPServer and its own validation, not agent-framework-hosting-mcp. P2/P5: preserve server rejection tests; no hosting adapter migration. |
| [#9788](https://github.com/microsoft/agent-framework/pull/9788) | Deduplicate upstream CodeAct FIDES warnings | No direct migration | Upstream Hyperlight/Monty warning state is not our CodeAct bridge. No warning suppression added. |
| [#9763](https://github.com/microsoft/agent-framework/pull/9763) | Correct Ollama streaming tool-call finish reason | Adopt and qualify | Cachebench's Ollama route is used. The terminal chunk may lack tool calls even when earlier chunks contain them. P3: chunk sequence with earlier tool calls, terminal stop, explicit length reason and early-close ownership. |
| [#9987](https://github.com/microsoft/agent-framework/pull/9987) | Prefer injected Azure OpenAI deployment over ambient alias | Qualify | Current factories pass model explicitly, which still wins. Hosts injecting an Azure SDK client without model get the corrected deployment precedence. P3: explicit model, injected deployment and environment fallback across Chat Completions/Responses; retain caller-owned SDK cleanup behavior. |
| [#9803](https://github.com/microsoft/agent-framework/pull/9803) | Pair empty local shell calls and replay local output | Qualify history boundary | Our shell kind is a regular FunctionTool, not the upstream local shell executor. However the generic Core continuation helper changes and compaction can encounter shell content. P3: paired shell call/result, local-result marker serialization, mixed tool batches and service continuation without duplicate or orphaned outputs; no automatic adoption of provider-native shell. |
| [#9483](https://github.com/microsoft/agent-framework/pull/9483) | Kill stateless shell process groups on timeout | No direct migration | agent-framework-tools is not used. Our shell kind, router and backends own sandbox process cleanup. Retain backend timeout/cleanup evidence; do not claim this fixes native sandbox process trees. |

### 1.25 release preparation and reconciliation

| Item | Assessment and action |
|---|---|
| [#10151](https://github.com/microsoft/agent-framework/pull/10151) | Release preparation sets the 44-package batch, records seven breaking migrations, aligns pins and incorporates four unmerged dependency updates. It also restores declarative `cache_max_size` as an ignored deprecated keyword; that does not restore shared protocol sessions or handler `aclose()`. The SQL Server assertion fix is upstream test-only. Use the final tagged state for P1. |
| M1: root/Core standard cohort | The umbrella now exactly pins Core 1.25.0 and Core's `all` extra advances its adapter cohort. This repo installs bare Core and selected providers. Do not replace that with the umbrella or install every optional integration. |
| M2: new Core floors | AG-UI, declarative, Foundry hosting and OpenAI require Core >=1.25 for actual new APIs. Only OpenAI is in our selected provider set. Foundry/Mistral/Ollama retain >=1.24; distinguish their new versions from a mandatory new Core floor. |
| M3: exact hosting pins | Hosting A2A/MCP/Responses/Telegram pin their co-released hosting artifact. None is declared here; no local hosting pin change. |
| [#9617](https://github.com/microsoft/agent-framework/pull/9617) / source commit `cf734bc4` | PR metadata says closed unmerged, and #10151's narrative excludes it from the changelog. Nevertheless, the tag-range commit and tagged/published `_types.py` contain its concrete response-overview examples, unlike 1.24. Record the source-present documentation change without calling the PR merged. No runtime/API migration. |

The following 43 tag-range PRs are also accounted for. They are outside Python runtime adoption; exclusions are based on changed paths, not just title prefixes.

| PR | Tag-range subject | Reason / disposition |
|---|---|---|
| [#9648](https://github.com/microsoft/agent-framework/pull/9648) | .NET: Pin the documentation pages of the RAG Step02 sample to a commit | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9845](https://github.com/microsoft/agent-framework/pull/9845) | .NET: fix(dotnet): handle parse-value conversion failures consistently | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9894](https://github.com/microsoft/agent-framework/pull/9894) | .NET: ci: adds a script to bump the release version | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9893](https://github.com/microsoft/agent-framework/pull/9893) | chore: bumps release version for dotnet | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#8756](https://github.com/microsoft/agent-framework/pull/8756) | .NET: Add GA computer tool support to Foundry and Foundry hosting | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9935](https://github.com/microsoft/agent-framework/pull/9935) | chore(deps): bump github/codeql-action/autobuild in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9936](https://github.com/microsoft/agent-framework/pull/9936) | chore(deps): bump github/codeql-action/analyze in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9934](https://github.com/microsoft/agent-framework/pull/9934) | chore(deps): bump github/codeql-action/init in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9933](https://github.com/microsoft/agent-framework/pull/9933) | chore(deps): bump astral-sh/setup-uv in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9932](https://github.com/microsoft/agent-framework/pull/9932) | chore(deps): bump actions/setup-python in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9873](https://github.com/microsoft/agent-framework/pull/9873) | .NET: fall back when a restored agent mode is no longer configured | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9773](https://github.com/microsoft/agent-framework/pull/9773) | fix: preserve GitHub Copilot reasoning token usage | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9899](https://github.com/microsoft/agent-framework/pull/9899) | .NET: [BREAKING] align workflow span name with OpenTelemetry conventions | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9901](https://github.com/microsoft/agent-framework/pull/9901) | ci: adds a skill for the release notes | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9995](https://github.com/microsoft/agent-framework/pull/9995) | .NET: Update A2A SDK to 1.0.0-preview4 | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9738](https://github.com/microsoft/agent-framework/pull/9738) | .NET: Preserve conversation continuity after OAuth consent | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9883](https://github.com/microsoft/agent-framework/pull/9883) | Stale PR closure workflow/script | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#9813](https://github.com/microsoft/agent-framework/pull/9813) | .NET: Preserve citations in OpenAI Responses hosting | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10037](https://github.com/microsoft/agent-framework/pull/10037) | .NET: [BREAKING] Postpone task support in A2A hosting | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9973](https://github.com/microsoft/agent-framework/pull/9973) | .NET: [BREAKING] CodeAct: redact host-tool diagnostics by default | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10046](https://github.com/microsoft/agent-framework/pull/10046) | chore: promote shipped .NET APIs | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10048](https://github.com/microsoft/agent-framework/pull/10048) | Fix Issue Planning workflow by recompiling with gh-aw v1.0.1 | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10064](https://github.com/microsoft/agent-framework/pull/10064) | chore(deps): bump j178/prek-action in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10063](https://github.com/microsoft/agent-framework/pull/10063) | chore(deps): bump actions/create-github-app-token in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10062](https://github.com/microsoft/agent-framework/pull/10062) | chore(deps): bump danielpalme/ReportGenerator-GitHub-Action in / | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10070](https://github.com/microsoft/agent-framework/pull/10070) | docs: fix spelling typos in decision records | Shared design-record spelling only; includes the compaction ADR but changes no contract. |
| [#10095](https://github.com/microsoft/agent-framework/pull/10095) | Update approval samples to include reason | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9722](https://github.com/microsoft/agent-framework/pull/9722) | .NET: [BREAKING] fix: scope workflow MCP clients to invocations | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10100](https://github.com/microsoft/agent-framework/pull/10100) | Allow configuring Foundry toolbox token scope | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10099](https://github.com/microsoft/agent-framework/pull/10099) | .NET: Fix per message user mapping in purview | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10098](https://github.com/microsoft/agent-framework/pull/10098) | fix: renames azure agent to foundry agent | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9841](https://github.com/microsoft/agent-framework/pull/9841) | .NET: [BREAKING] fix(dotnet): require allowed HTTP hosts | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10065](https://github.com/microsoft/agent-framework/pull/10065) | .NET: Preserve non-ASCII text in hosting tool payloads | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10109](https://github.com/microsoft/agent-framework/pull/10109) | Reject unscoped Mem0 memory deletion | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#9994](https://github.com/microsoft/agent-framework/pull/9994) | .NET: remove nonstandard workflow payload telemetry | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10108](https://github.com/microsoft/agent-framework/pull/10108) | docs: fix dont typos in ChatCompletions comments | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10113](https://github.com/microsoft/agent-framework/pull/10113) | .NET: [BREAKING] Simplify A2A agent creation and payload handling | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10121](https://github.com/microsoft/agent-framework/pull/10121) | Ensure enable file hooks defaults to false | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10122](https://github.com/microsoft/agent-framework/pull/10122) | .NET: Use source-generated logging in Foundry Hosting | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10124](https://github.com/microsoft/agent-framework/pull/10124) | .NET: Clarify Foundry toolbox startup and readiness XML documentation | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |
| [#10045](https://github.com/microsoft/agent-framework/pull/10045) | Add manual agentic issue triage workflow | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10144](https://github.com/microsoft/agent-framework/pull/10144) | Enable automatic issue routing | Upstream repository automation/tooling; not shipped Python behavior and no local workflow change required. |
| [#10069](https://github.com/microsoft/agent-framework/pull/10069) | .NET: fix(dotnet): parse declarative workflow formulas and values with InvariantCulture | .NET-only implementation, tests or samples; this Python suite does not consume these assemblies. |

### 1.25 verification completed

This section records the original assessment before implementation. See the linked adoption record for subsequent source changes and validation.

The baseline environment came from `uv sync --locked` at the pinned repository revision. Candidate wheels were installed into a separate directory with `--no-deps` and placed first on `PYTHONPATH`; actual Core import location and all five distribution versions were checked. Other dependencies stayed at the locked baseline. This intentionally bypasses the repository's current `<1.25` metadata and is **not a supported resolution or clean consumer install**.

| Check | Result | Boundary |
|---|---|---|
| Baseline focused suite, Windows CPython 3.13.12 | 3,496 passed, 7 warnings; 235.36 seconds | All compaction and Cachebench tests, sandbox file provenance/framework glue, MAF 1.24 compatibility and framework approval contracts |
| Candidate cohort, identical selection | 3,496 passed, 7 warnings; 225.63 seconds | Same source and selected tests with the five candidate wheels; no product adaptation needed by this selection |
| Additional host-file integrity, CodeAct FIDES refusal and result-integrity suites, both cohorts | 286 passed each; 10 warnings each; baseline 2.99 seconds, candidate 2.93 seconds | Existing real-middleware/in-process coverage; combined with the focused selection, 3,782 tests passed per cohort |
| Published Core `_sessions.py` and `_compaction.py` | Both byte-identical to the 1.24.0 wheel | Private implementations unchanged; callers and middleware still require qualification |
| Core tagged `_sessions.py` comparison | Identical across 1.24/1.25 | Independent source check of session implementation |
| Release reconciliation | 77/77 note PRs, 118/118 tag commits, all 122 distinct references accounted for, M1-M3 covered | Complete inventory/classification, not complete runtime qualification |
| Candidate wheel metadata | Core 1.25.0; OpenAI 1.18.1; Foundry 1.16.1; Mistral/Ollama 1.0.0b261106; MCP stays 2.2.0 | Publication/install evidence for the used cohort only |
| Assessment validation | Documentation references hold; diff whitespace check passes; automated ledger check confirms all 77 release rows and 122 total PR rows | Document integrity only; not an implementation gate |

The first baseline attempt used importlib test collection, which broke two existing sibling-test imports, and sandboxed execution could not fetch tiktoken encoding data. Two sandboxed integrity-suite processes stalled before producing results and were stopped; their completed replacements are the 286-test runs above. These setup failures are not framework regression evidence. The completed baseline/candidate pair uses normal repository collection and permitted encoding download. Elapsed times are not a performance comparison.

Retained local evidence is in `.artifacts/maf-1.25-assessment/` in the assessment worktree: `upstream-inventory.json`, `assessment-verification.json`, `verify_assessment.py`, `baseline-focused.log`, `candidate-focused.log`, `baseline-labels.log`, `candidate-labels.log`, `candidate-metadata.log` and the isolated candidate wheel contents. Raw logs contain local execution paths and are not suitable for public publication unchanged.

Not verified by this assessment: the complete repository gate; new adversarial FIDES/approval regressions proposed below; the full real-HTTP MCP ownership suite; fresh strict typing on all packages; Python 3.12/3.14; Linux; a changed lockfile or clean resolver installation; all optional extras; built-wheel compatibility and release gates; hosted CI; real model calls; native Docker/AKS/Hyperlight/WSLC qualification; live cache economics or recall quality; production stores; publication of this suite; or production readiness. No dependency declaration, generated release file or product source was changed.

### 1.25 ordered adoption plan

Original plan at assessment time. Delivery is tracked by [#2641](https://github.com/sokolaidev/maf-extensions/issues/2641), with implemented coverage and remaining qualification boundaries in the adoption record. The proposed state in this historical table is not a claim about the implementation PR.

| Order | Work and concrete scope | Acceptance criterion | State |
|---|---|---|---|
| P0 | Qualify FIDES #9875 first at real FunctionMiddlewarePipeline/Agent boundaries. Cover every sandbox kind's emitted policy, supported confidentiality values, hidden string/list/object arguments, exact rewritten positions and both provenance middleware orders. Exercise invalid/miscased policy, a blocked hidden value with a side-effecting validator, changed tool/policy/session/runtime identity, approved normalization, retry, cancellation and lone-surrogate JSON/MessagePack persistence. Review the now-stale source rationale that invalid upstream policy values are silently treated as absent. | Invalid declarations refuse before validators or tool bodies; benign suite metadata remains valid; approved authority cannot be swapped; guidance remains readable, derived content restricted, confidentiality never lowered, no trust promotion from missing provenance. Failed/refused writes preserve bytes and conservative records. Keep strict refusal; fix our assumptions if necessary. | Proposed; existing provenance/approval selection passed |
| P1 | Coordinate all twelve Core declarations to `>=1.25.0,<1.26`, root development provider constraints, Cachebench extras and `uv.lock`. Guarantee OpenAI 1.18.1, Foundry 1.16.1 and the Ollama beta containing their fixes. Decide whether to retain Mistral's older admitted floor with a separate floor run or raise it for one measured cohort. Update release compatibility documentation after evidence. Preserve Python/MCP bounds and unrelated dependencies. | Fresh solve including all Cachebench provider extras; actual versions recorded; framework ceiling and locked-framework checks consistent. No silently impossible dependent ranges. | Proposed |
| P2 | Add installed-Core MCP regressions for #10042/#10147/#10136 alongside existing service/ownership coverage. Test exact discovery boundaries and failed-refresh retention of tools, parameters, annotations and approvals; callable-policy refresh; explicit timeouts; same-identity overlap; different-identity isolation; redirects, cancellation, shared recovery and explicit close. Keep sample 21's 60 seconds and qualification fixtures' 240 seconds. | A real HTTP test proves overlap and identity isolation; failed discovery leaves the whole previous snapshot; old recovery cannot reopen an explicitly closed client; borrowed resources remain usable. Neither discovery item limits nor admission/recovery timers are described as an outer payload cap or end-to-end deadline. | Proposed |
| P3 | Add provider/Core regressions for #9403/#9679/#9681/#8960/#9852/#9943/#9094/#9931/#9789/#9763/#9987/#9803. Use installed adapter parsers and synthetic SDK events/transports; test ConfiguredClient and direct BaseChatClient paths. Include metadata-only stream updates, dynamic schemas, cancellation/finalization, termination-carried results, rich tuples, integer normalization, citation/reasoning ownership, empty shell pairs and service continuation. | Correct final messages/metadata and actual outgoing payloads; no orphaned or duplicated results; rejected recall turns do not persist; annotated results refuse lossy rewriting; client ownership remains intact. Fresh cache benchmark baselines distinguish schema-order changes from strategy effects. | Proposed |
| P4 | Turn the qualified scope into a repository adoption issue/implementation PR if delivery is requested. Derive prepared sandbox/compaction and any required dependent floors from the actual final release scope; move floor and ceiling together where required. Update every tracking row for issues the PR closes and cite its eventual PR number. | One coherent installable change; honest Conventional Commit title covering every touched package; incidental changes split out; PR template and exact validation limitations recorded. No manual versions, changelogs or release manifest edits. | Proposed; no issue/PR published by this assessment |
| P5 | Run `uv sync` and `uv run poe gate`; documentation checks; `poe md-blocks`; `poe sample-floors` if samples change; tracker checker where applicable. Build all 21 wheels, check metadata/source correspondence, clean-install each and the combined suite with provider extras, then run declared/published-core compatibility checks. Run supported Python versions/platforms in hosted CI against the exact PR head. | Full candidate offline suite and required checks green, or a clearly retained/reproduced baseline issue addressed before claiming green. Branch-core overrides prove branch compatibility only; publication resolves real wheel requirements from the index. | Proposed |
| P6 | Perform bounded live qualification on Azure Chat Completions, Azure Responses, cloud Foundry, Mistral, Ollama and OpenRouter where configured. Include streaming, multi-tool turn, recall/compaction, save/restore, cancellation and a representative sandbox tool; run Microsoft Learn MCP and OpenClaw ownership paths. Then hand release sequencing to the maintainer. | Retained model/config/source/version identities and cleanup evidence; publish sandbox/compaction prerequisites before dependent uploads, Bicep before MCP when its bound requires the new artifact, and Cachebench after its compaction requirement is available. Merge the samples floor update only after every referenced dependent is published. | Proposed; live and publication unqualified |

Use one measured framework band rather than claiming untested cross-minor support. If supporting 1.24 and 1.25 simultaneously becomes a requirement, that is a separate compatibility matrix with assertions for both middleware contracts; it is not satisfied by widening a version string.

Do not infer a store migration from unchanged `_sessions.py`. FIDES durable encoding and response schema metadata changed, so retain original serialized fixtures and test old-to-new restore, new-to-new restore and the intended rollback shape before touching retained host data. No destructive Redis or memory-store migration is proposed.

The existing repo approval boundary remains: prepare and review the adoption PR, then stop. Merging, tagging, deployment approval and publishing remain maintainer actions.

### 1.25 release-bullet crosswalk

Numbers refer to the 46 bullets in the tagged 1.25 changelog. Each PR is assessed once in the ledger; this table makes grouped bullets auditable.

| Bullet | Assessed items |
|---|---|
| 1 | #10136 |
| 2 | #9948 |
| 3 | #10042 |
| 4 | #10035 |
| 5 | #9900 |
| 6 | #8662 |
| 7 | #9322 |
| 8 | #9971 |
| 9 | #10096 |
| 10 | #10143 |
| 11 | M1 |
| 12 | M2 |
| 13 | M3 |
| 14 | #10103 |
| 15 | #9783, #9897, #10105 |
| 16 | #9735, #9740, #9758, #9781, #9906 |
| 17 | #10032, #10086, #10038 |
| 18 | #9937, #10117, #10118, #10119, #10120 |
| 19 | #9947 |
| 20 | #10115, #10140 |
| 21 | #10129, #10128 |
| 22 | #10147 |
| 23 | #9403, #9679 |
| 24 | #9681, #8960, #9852 |
| 25 | #9943, #9942, #9998 |
| 26 | #9294, #9547, #9387 |
| 27 | #9875 |
| 28 | #9094 |
| 29 | #9770 |
| 30 | #9859, #9898 |
| 31 | #9972, #10106 |
| 32 | #9996, #9982, #9985, #9983, #9993, #9977 |
| 33 | #9796, #9967 |
| 34 | #9984, #9969 |
| 35 | #10005 |
| 36 | #9712 |
| 37 | #9931, #9789, #9981, #9991 |
| 38 | #9745 |
| 39 | #10090, #10088 |
| 40 | #10149 |
| 41 | #10036 |
| 42 | #9788 |
| 43 | #9763 |
| 44 | #9987 |
| 45 | #9803 |
| 46 | #9483 |
