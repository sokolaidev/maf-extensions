# maf-sandbox: sandboxed code execution for MAF agents

> An introduction to the suite — what it is, how it attaches to an agent, and what a host gets for wiring it in. It assumes no prior knowledge of Microsoft Agent Framework. [`docs/design/`](design/) carries the depth: [`sandbox-architecture.md`](design/sandbox-architecture.md) for the surface as built, [`two-axis-sandbox-policy.md`](design/two-axis-sandbox-policy.md) and [`files-out.md`](design/files-out.md) for the decisions behind it.

## Microsoft Agent Framework, in a paragraph

[Microsoft Agent Framework](https://aka.ms/AgentFramework) (MAF) is an open-source SDK for building agents on top of a chat model. An **agent** has instructions, a **thread** of conversation, and a set of **tools** — ordinary functions the model may call, whose results come back into the conversation. Between the model and those tools sits a **middleware** chain, which is where a host puts what it cannot let the model decide: approval gates, information-flow policy, budgets, telemetry. The framework's central bet is that anything consequential an agent does, it does through a tool call — because that is the one place a host can see it and stop it.

## The problem

Agents increasingly write code: an infrastructure template, a short Python program, a data transformation. There is exactly one way to find out whether generated code is correct, and that is to run it. Ask the model to check its own work and what comes back is a fluent guess; run the compiler and what comes back is the compiler's answer. The difference is not one of degree — a model that reads its own output and agrees with itself has added no information at all.

So the agent needs somewhere to run things, and the obvious place is the one place it must not be. On a managed platform the host process environment *is* the credential: a container receives its managed identity as environment variables sitting next to whatever provider keys the host holds, and a child process inherits them. Scrubbing the environment of a subprocess is not a control, and declining to install a cloud CLI is not a control either — any HTTP request from that process reaches the same token endpoint. Either the boundary is a real one or there is no boundary.

That leaves a host choosing between an agent that cannot verify its own output and an agent that runs model-authored code with the host's authority. This suite exists so that it does not have to choose.

## What the framework already does, and where it stops

Three answers ship with or around MAF today, and each is the right choice for some workload.

**A provider-hosted code interpreter.** A chat client implementing `SupportsCodeInterpreterTool` hands you a tool through `get_code_interpreter_tool()`, and the provider runs the code in its own sandbox. There is nothing to operate, and for *plot this dataframe* it is hard to beat. What it cannot do is run *your* toolchain: the environment belongs to the provider, so a pinned compiler, a rendering binary or a private package feed is not on the menu, and neither is an egress policy you wrote or a boundary you can point at during an audit. Hosted Python services such as Azure Container Apps dynamic sessions sit in the same place for the same reason — a fixed environment rather than an image you build.

**In-process CodeAct runtimes.** `agent-framework-hyperlight` runs a workload in a hypervisor-backed micro-VM *inside* the host process; `agent-framework-monty` runs it in an embedded interpreter with no filesystem and no network at all. Both are fast, both support host tool callbacks — the CodeAct differentiator — and Monty's no-I/O construction is a stronger containment claim than most containers can make. What neither offers is an operating system: no shell, no package manager, no CLI, no filesystem that survives a fix-and-retry loop. An agent whose job is to run a compiler is outside their scope by design.

**Everything else is per-application integration.** Which is where the gap stops being a matter of coverage and becomes structural.

### The gap

There is no shared contract in core that a third execution environment can implement. Hyperlight and Monty each define their own provider and their own `*ExecuteCodeTool`, and each defines its own `FileMount` — two packages solving one problem and sharing no vocabulary. Hosted interpreters are provider-side by construction. There is no `agent_framework.sandbox`.

The consequence is not that a remote sandbox cannot be integrated; it is that everyone integrating one rebuilds the same layer underneath the tool. In the application these packages were extracted from, the workload was a few hundred lines and the harness around it — backend lifecycle, keying, disposal, egress wiring, injection handling — was roughly three times that, none of it specific to the application. That layer is where the quiet bugs live: the billable sandbox leaked because the delete arrived on a replica that had not created it; the confused deputy from a sandbox key the model could influence; the command string built by interpolation; the cold start paid every round because a warm sandbox was never reused.

The framework's own tracker shows the same shape from four directions — a remote micro-VM CodeAct backend that had to mimic the Hyperlight shape by convention and was closed as not planned ([#7311](https://github.com/microsoft/agent-framework/issues/7311)); a request for a general sandbox provider abstraction for remote HTTP services ([#6490](https://github.com/microsoft/agent-framework/discussions/6490)); a local-machine executor bridge asking for a more idiomatic extension point rather than its own seam ([#7319](https://github.com/microsoft/agent-framework/discussions/7319)); and a first-party issue on executing skill resources in sandboxes that opens by observing there are already several ways to make one ([#6475](https://github.com/microsoft/agent-framework/issues/6475)). [#7568](https://github.com/microsoft/agent-framework/issues/7568) is the umbrella feature request collecting them, and this suite is its reference implementation.

### What else is out there

**Sandbox-as-a-service SDKs** — E2B, Daytona and similar — solve the genuinely hard part, which is the isolated machine, and they solve it well. What they hand you is a client rather than a contract: a tool written against one is written against that vendor, and moving it to another, to a container on a CI runner, or to a fake in a unit test is a rewrite rather than a configuration change. None of them can decide whether a given boundary is strong enough for your deployment, because that is a question about your host and not about their service.

**Container executors** of the kind familiar from the AutoGen line run code in a local Docker container. That is the right tool on a developer's machine, and it carries no isolation claim a deployment can enforce — a shared kernel is not what you want between model-authored code and a process holding managed-identity credentials.

**Hardening the in-process path** — a locked argument list, no shell, a scrubbed environment — is worth doing and is never sufficient. It answers an agent misusing the tool as designed. It does nothing about the tool being used to run something else, because the two controls that would hold that line, a different user and no reachable token endpoint, are both absent inside your own process.

What none of these provides is the part this suite is: a contract that lets a workload be written once, and a policy that lets the *host* decide which boundaries it is willing to accept.

## What the suite is

A backend-neutral protocol for giving agent-written code **somewhere else to run**, reached as an **ordinary tool call**.

That second half is the hinge the whole design turns on. The *work* ships out to an isolated sandbox; the tool *call* stays in the host process, so the middleware chain still sees it, still gates it, and still classifies what it returns. The alternative shape — exposing the sandbox as a remote *agent* — leaves that security context entirely, and everything coming back has to be re-treated as untrusted ingress. Tool rather than agent is a security decision, not an ergonomic one.

Three roles, and the separation between them is what everything else rests on:

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

- **The protocol and the router** — `maf-sandbox`. The vocabulary (`SandboxKey`, `SandboxSpec`, `Sandbox`, `SandboxBackend`, `Isolation`, `Capability`) and the policy deciding whether a given backend may serve a given request. Its protocol modules import nothing but the standard library: a seam that depends on the things it separates is not a seam.
- **A backend** — *where* code runs. Azure Container Apps Sandboxes, a plain Docker container, a `wslc` container on a developer's own machine, or the in-process fake in `maf_sandbox.testing`. A backend declares what it can and cannot provide, and is held to it.
- **A kind** — *what work* runs. `bicep_validate` compiles agent-authored Bicep; `execute_code` runs a short program the model wrote and returns what it printed. A kind is a MAF tool with a sandbox behind it, written against the protocol alone.

Because kinds and backends never import each other — and tests fail if they begin to — a kind is portable by construction. The same `bicep_validate` runs in a microVM in production, a Docker container on a CI runner, and an in-process fake in a unit test, unchanged.

## What the suite is not

**Not an isolation technology.** It competes with no container runtime and no hypervisor; it is the seam and the policy above them. What it adds is making isolation *declarable*, *matchable against what a workload asked for*, and *enforceable before anything runs*. The analogy that fits is a database API and its drivers — the value is the contract, and that everyone on either side of it can be replaced.

**Not a sandbox provider.** Nothing here provisions infrastructure, builds an image or runs a service. A host brings a backend, and building and publishing the image a workload runs in is a deployment concern that stays outside the protocol on purpose.

**Not a replacement for the in-process runtimes.** If a workload fits an embedded interpreter or an in-process micro-VM, that is faster, cheaper and simpler than any remote sandbox, and it keeps host tool callbacks that remote services mostly cannot offer. The protocol's ambition is to have room for both ends of that range — a shared vocabulary in which an interpreter and a remote micro-VM can each declare what they truthfully are — rather than to argue that one of them wins.

**Not a file-writing library.** A kind can bring artifacts back out of a sandbox, but the landing callback belongs to the host, and the library never writes to the host filesystem itself. A kind that wrote where the agent's own file tools write would have handed model-authored bytes an approval gate they never passed.

**Not a guarantee about a backend you supply.** The router enforces what a backend *declares* against what a workload *asked for*. Whether a backend's declaration is true of the system underneath it is that backend's claim to make and its documentation's to justify — the protocol makes the claim explicit and checkable, which is a different thing from making it true.

## How it attaches to a MAF agent

Exactly one module imports `agent_framework`, and it is deliberately not re-exported from the package: `import maf_sandbox` stays cheap and framework-free for a backend author or a workload's own test suite, while a host reaches the conveniences by name. Both halves are pinned by tests.

A host wires three things.

**The tools.** A kind's factory returns a plain list of MAF tools, which go onto the agent like any others:

```python
router = SandboxRouter([AcasSandboxBackend(config)])
context = make_workspace_context(list_workspace, lambda: scope, lambda: thread_id)
tools = make_bicep_tools(router, store, agent_dir, context, image=image)

agent = Agent(client=client, name=agent_dir, instructions=..., tools=tools)
```

**The request context.** `make_workspace_context` takes *callables*, read per call, rather than values. A sandbox is keyed by `(scope, thread_id, agent_dir)`, and a host that builds one agent and serves many conversations with it would — if the scope and thread were captured at construction time — let one conversation address another conversation's sandbox. Nothing in this stack accepts a scope, a thread id or a file path from the model: the workspace listing is the boundary that decides what a name is allowed to resolve to.

**Disposal.** `SandboxPurger` participates in thread deletion, so deleting a conversation takes its sandboxes with it, and `dispose_scope` deletes by service-side label — which reclaims sandboxes the calling replica never created.

Two behaviours are worth knowing before the first call, because both are deliberate and neither is what a host would arrive at by accident:

- **Nothing configured attaches nothing.** With no backend, a kind's factory returns `[]` — not a tool that fails when the model calls it. The host keeps its ungrounded behaviour with no half-attached error path, and the model is never shown a capability it does not have.
- **A backend that cannot honour the spec raises.** That is a misconfiguration rather than a choice, and a quiet degrade would ship a workload with containment it does not have. The distinction is the rule: absence is silent, dishonesty is loud.

## What it buys

**Truth in place of plausibility.** The agent stops reporting that a template looks valid and starts reporting what the compiler said. Everything else here is what it costs to get that safely.

**Isolation declared rather than hoped for.** A host sets a minimum isolation floor on an ordered ladder — `process`, `runtime`, `container`, `hardened_container`, `microvm`, `vm` — and a backend below it is refused *at construction*, not at the first tool call, so a misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe. The default is `microvm`, the production posture; a workload's spec may raise the floor and can never lower it.

**Capabilities that mean something.** A backend declares what it can do — execute a command, take files in, read a declared file back out, enumerate a directory, and more — and the router matches that against what the workload asked for, refusing rather than degrading. One rule keeps the list honest: *name the backend that lacks it; if none does, it is a comment, not a capability.* That is why reading a declared output and listing a directory are separate capabilities, since Docker has no engine-level listing primitive — and why widening a write to accept bytes got no capability at all.

**Egress closed unless opened.** A spec names the hosts its work may reach, and a backend that cannot confine egress to that list is refused. A spec silent about egress gets the closed configuration, never the open one.

**Failures that do not leak.** When a sandbox is unavailable the model receives a fixed sentence saying the run degraded, and nothing else. The provider's own message — endpoint, subscription, tenant — goes to the log instead, because a tool result is persisted into the transcript.

**Portability, and no lock-in.** Cloud microVMs in production, Docker on a CI runner, a container on a laptop, an in-process fake in the test suite. A new backend is a sibling package implementing one protocol; a workload written against that protocol does not learn about it.

## Where to read next

- [`samples/`](../samples/) — the `app` box above, wired end to end, each small enough to read in one sitting.
- [`docs/design/sandbox-architecture.md`](design/sandbox-architecture.md) — the surface as built: the vocabulary, the checks the router runs, keying, lifecycle.
- [`docs/design/two-axis-sandbox-policy.md`](design/two-axis-sandbox-policy.md) — the isolation ladder and the capability axis, and where known systems sit on them.
- [`docs/design/files-out.md`](design/files-out.md) — getting artifacts back out, and why that is not the mirror image of putting them in.
- Each package's own README, for its configuration, its guarantees and its limits.
- [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) — the upstream feature request this suite is the reference implementation of.
