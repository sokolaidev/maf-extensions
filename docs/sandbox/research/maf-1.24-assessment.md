# Agent Framework Python 1.24.0 assessment

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

