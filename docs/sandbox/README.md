# Sandboxed tools for agents

`maf-sandbox` connects Microsoft Agent Framework (MAF) tools to sandbox backends. It separates what a tool needs from where its work runs.

## Introduction

### The problem

Agents can write code, but checking it needs real execution. A compiler can find an invalid template. A Python runtime can test a calculation or process a file.

That work handles model-generated code and untrusted inputs. Running it with the application's permissions can expose files, credentials and network services. A child process alone does not remove those permissions.

Isolation is only part of the job. The application must control who can use a sandbox and what may enter or leave it. It also needs rules for cleanup and for using the results.

### Available solutions

| Approach | What it provides |
|---|---|
| [Provider-hosted code interpreters](https://learn.microsoft.com/en-us/agent-framework/agents/tools/) | The model service runs code through its own tool interface. |
| MAF's [Hyperlight](https://github.com/microsoft/agent-framework/tree/main/python/packages/hyperlight) and [Monty](https://github.com/microsoft/agent-framework/tree/main/python/packages/monty) tools | Python execution through a specific runtime, with agent tool integration. |
| Sandbox services, such as [E2B](https://docs.e2b.dev/) and [Modal](https://modal.com/docs/guide/sandboxes) | Environments the application creates and controls through a provider SDK. |
| Containment SDKs, such as [MXC](https://github.com/microsoft/mxc) | Common configuration for several operating-system containment backends. |
| [Deep Agents](https://docs.langchain.com/oss/python/deepagents/sandboxes) and [Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit/tree/main/agent-governance-python/agent-sandbox) | Shared interfaces across sandbox providers, with agent integration or governance features. |

These approaches overlap. Shared APIs reduce provider-specific code. An application still needs rules for its own workloads and the results it sends back to the model.

### What maf-sandbox adds

`maf-sandbox` supplies that integration for MAF. A **kind** defines the workload. A **backend** supplies the execution environment. Their shared contract covers four concerns:

- **Workload reuse.** [Kinds](kinds/README.md) use one protocol and contain no backend imports. A workload can move between compatible backends without provider-specific code in the kind.
- **Checks before execution.** The [router](policy-isolation.md) checks required operations, the host's minimum isolation and network rules. It refuses an unsupported combination.
- **Host control.** Outer tool calls pass through framework policy. [Identity](architecture.md#keys-and-storage) comes from trusted request context. [File access](hosts.md) and [cleanup](tool-call.md) follow shared rules.
- **Useful, labelled results.** The [result contract](information-flow.md#the-result-contract) separates completion and a declared answer from raw workload output. The model can read the answer while untrusted text stays separately labelled.

A Bicep tool can use Docker locally or Azure Container Apps Sandboxes (ACAS). Each setup must satisfy the workload requirements and its host's policy. Hyperlight cannot run the Bicep compiler, so the router refuses it for this kind.

The suite connects sandbox implementations; it does not provide isolation by itself. A backend must establish the boundary and behavior it declares.

## Choose the workload and backend

![Bicep, CodeAct, draw.io and Terraform/OpenTofu kinds describe their work through one SandboxSpec and router contract. Backends implement that contract for ACAS, Docker, WSLC, Hyperlight or an in-process test fake. Kinds and backends do not import each other. A workload can use another backend only when that backend satisfies its requirements; shell workloads and language-runtime workloads have different needs.](assets/suite-shape.svg)

| Kind | Work |
|---|---|
| [Bicep](kinds/bicep.md) | Compile and lint Bicep templates and parameter files |
| [CodeAct](kinds/codeact.md) | Run Python with optional file inputs, artifacts and host tools |
| [draw.io](kinds/drawio.md) | Validate and lay out editable diagrams |
| [Terraform / OpenTofu](kinds/terraform.md) | Validate offline and optionally format configuration |

| Backend | Execution environment |
|---|---|
| [ACAS](backends/acas.md) | Azure Container Apps Sandboxes, with a POSIX guest |
| [Docker](backends/docker.md) | Container engine, with backend-enforced file and network rules |
| [WSLC](backends/wslc.md) | WSL container engine, with a narrower supported surface |
| [Hyperlight](backends/hyperlight.md) | Packaged Python runtime behind a local micro-VM boundary |
| [In-process fake](backends/in-process.md) | Offline tests, without real containment |

Use the [backend comparison](backends/README.md) to check compatibility. The default router floor is `MICROVM`; Docker, WSLC and test fakes require an explicit lower floor. The kind's required capabilities, guest family and egress mode must also match.

## What runs where

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

The agent, middleware and tool body run in the host. The tool sends commands, code or files to the guest. The backend returns execution results and selected files.

![The host contains the agent, framework middleware, tool body and router. Workload inputs cross into the sandbox, which holds the working directory and program. Results return to the host, and network access follows the selected egress policy. The tool call stays on the host's framework path while its work runs across the execution boundary.](assets/sandbox-flow.svg)

The host supplies request identity, credentials, images, storage destinations and lifecycle policy. Optional guest-to-host tools use a separate, explicitly configured registry. They do not pass through the middleware that checked the outer tool call.

## Tools, content and labels

Isolation controls where work executes. Information-flow policy controls how its inputs and results can be used.

![Source tools declare result integrity and confidentiality. Each returned content item has an effective label and reaches the model as visible text or a hidden reference. The model's later calls pass through host policy, which checks the destination tool's accepted integrity and confidentiality. Hidden content still affects confidentiality, and sandbox isolation does not make guest output trusted.](assets/information-flow.svg)

The [result contract](information-flow.md#the-result-contract) defines separate completion, verdict, trusted-text and workload-output items. The wrapper owns their labels. Backend output bytes alone do not establish trust.

## Connect to a MAF agent

Wire the tools, per-call request context and disposal. This host scaffold assumes the client, store, request identifiers, image and logger are already configured:

```python
router = SandboxRouter([AcasSandboxBackend(config)])
record = FileStoreProvenance()  # what the host knows about the bytes in `store`
context = make_caller_context(
    lambda s: list_all_files(s, provenance=record), lambda: scope, lambda: thread_id
)
tools = make_bicep_tools(router, store, agent_id, context, image=image)

agent = Agent(
    client=client,
    name=agent_id,
    instructions=...,
    tools=tools,
    # Record the integrity of agent-driven file writes.
    middleware=[file_store_provenance_middleware(record)],
)

# Dispose this conversation when the block ends.
async with router.scope(scope, thread_id) as disposal:
    response = await agent.run(prompt)
# Inspect the outcome from a host finally block when the turn can raise.
logger.info("reclaimed %d, still there: %s", disposal.disposed, disposal.undisposed)

# Also wire the host's conversation-delete path.
await SandboxPurger(router).purge_scoped_thread(scope, thread_id)
```

The kind factory returns ordinary MAF tools. With no backend configured, it returns an empty list. A configured backend that cannot serve the spec raises instead of attaching an unusable tool.

Scope and thread accessors are read for each call. They must use trusted request context, not model input or values captured for one conversation when building a shared agent.

`list_all_files(..., provenance=record)` supplies file names and their recorded integrity. The matching middleware records agent-driven writes. Without provenance, file integrity is unestablished. Workloads without a file channel use `list_no_files`.

The router defaults to disposal after active calls finish. Conversation-scoped sandboxes can stay warm only when the host enables reuse. `router.scope` purges when its block ends, including on failure; inspect its result to detect resources that remain.

Also wire `SandboxPurger` into conversation deletion. Call-scoped work is disposed at call end.

Host death needs a platform lifecycle policy or an independent operator sweep. No router in the dead process can complete that cleanup. See [operations](operations.md).

## Other integrations

| Package | Purpose |
|---|---|
| [OpenTelemetry](../../packages/maf-sandbox-otel/README.md) | Record logs, spans and metrics through host-configured providers |
| [Deep Agents](../../packages/maf-sandbox-deepagents/README.md) | Use the router for Deep Agents command and file tools; MAF kinds and labels are not part of that adapter |
| [TUI](../../packages/maf-sandbox-tui/README.md) | Inspect local application-owned instances and request coordinated deletion |

## Documentation map

| Page | Read it for |
|---|---|
| [Architecture](architecture.md) | Layers, core vocabulary, keys and framework integration |
| [Policy and isolation](policy-isolation.md) | Isolation levels, host floors and admission checks |
| [Capabilities](capabilities.md) | Backend selection, file methods, limits and confinement |
| [Guest platform and commands](guest-platform-and-commands.md) | Guest families, path rules and acquire-time probes |
| [Network](network.md) | Egress modes, allow entries and backend enforcement |
| [Host boundary](hosts.md) | Artifact sinks, host tools, authority and file integrity |
| [Information flow](information-flow.md) | Tool declarations, content labels and the four result items |
| [Tool-call lifetime](tool-call.md) | Binding, call and run ownership, concurrency and cleanup |
| [Execution output](exec-output.md) | Returned bytes, display text, caps and diagnostics |
| [Observability](observability.md) | Observer events, session state and observation limits |
| [Operations](operations.md) | Conversation purge and operator-owned retention |
| [Kinds](kinds/README.md) and [writing a kind](kinds/writing-a-kind.md) | Workload contracts and implementation |
| [Backends](backends/README.md) and [writing a backend](backends/writing-a-backend.md) | Provider contracts and conformance |
| [Samples](../../samples/) | Complete application wiring |
| [Research records](research/) | Explorations and proposals, with their original status |

Package READMEs cover installation and configuration. [Authoring guidance](../AUTHORING.md) defines the documentation structure.
