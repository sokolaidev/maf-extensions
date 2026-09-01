# maf-sandbox: sandboxed code execution for MAF agents

> An introduction to the suite — the problem it solves, how it relates to everything else in a crowded field, how it attaches to an agent, and what a host gets for wiring it in. It assumes no prior knowledge of Microsoft Agent Framework. The documents beside it carry the depth: [`architecture.md`](architecture.md) for the shape, [`policy-isolation.md`](policy-isolation.md) and [`capabilities.md`](capabilities.md) for the two axes the router matches on, [`network.md`](network.md) and [`hosts.md`](hosts.md) for the two boundaries around them, and [`kinds/`](kinds/README.md) and [`backends/`](backends/README.md) for what plugs into each end.

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

Sandboxing for agents is a crowded and fast-moving field, and most of it sits at a *different layer* rather than in the same place. Beyond the framework-native options above, three layers are worth separating, because anyone choosing tools needs to know which one they are shopping in.

**Containment systems — the boundary itself.** [MXC](https://github.com/microsoft/mxc), the Microsoft eXecution Container, is a cross-platform system for running untrusted code behind one versioned JSON schema and a TypeScript SDK, with ProcessContainer, Windows Sandbox, LXC, Bubblewrap, Seatbelt, micro-VM, Hyperlight and WSLC as interchangeable backends, and filesystem, network and UI policy over all of them. It is the same *idea* one layer down — many boundaries behind one description — which makes it a natural backend for a protocol like this one rather than a competitor to it. It is an early preview, and its own README currently asks that no MXC profile be treated as a security boundary yet. Below it sit the primitives themselves: Firecracker, gVisor, Kata, Hyperlight, Landlock, Seatbelt.

**Sandbox services — someone else's machine.** Azure Container Apps Sandboxes and dynamic sessions, E2B on Firecracker micro-VMs, Modal on gVisor, Daytona on containers. They solve the genuinely hard part and solve it well. What each hands you is a client rather than a contract: a tool written against one is written against that vendor, and moving it to another — or to a container on a CI runner, or to a fake in a unit test — is a rewrite rather than a configuration change.

**Governance layers — was this call allowed, and who made it.** Microsoft's [Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit) intercepts every tool call, message and delegation in deterministic code, evaluates a declared policy, and writes an audit record, alongside identity and reliability concerns, for any framework. That is a different question from this one and largely a complementary answer: MAF addresses it in-process through the middleware chain and `agent_framework.security`, which is exactly why this suite keeps a sandbox on the *tool* path where both can see it.

The toolkit also ships `agt-sandbox` — five interchangeable providers (Docker, Hyperlight, ACA Sandboxes, MXC, nono) behind one `SandboxProvider` ABC — which makes it the nearest neighbour to this suite, and the thing to look at first if governance is the primary need. The difference is what the abstraction is *of*. `SandboxProvider` abstracts a session that executes code, and the host chooses the provider. This protocol abstracts the sandbox a *workload* needs: a spec carrying the image, the egress allowlist, the declared outputs and the required capabilities, and a router that decides whether a given backend may serve it — refusing one below the host's isolation floor, or missing a capability, rather than degrading. Its unit is a tool attached to an agent, keyed to a conversation and disposed with it, rather than a session a caller opens and closes.

**And one thing that is not a layer at all: hardening the in-process path** — a locked argument list, no shell, a scrubbed environment — which is worth doing and never sufficient. It answers an agent misusing the tool as designed, and does nothing about the tool being used to run something else, because the two controls that would hold that line, a different user and no reachable token endpoint, are both absent inside your own process.

That the same nouns keep reappearing across all of these — a spec, an allowlist, a mount, a set of interchangeable backends — is the argument the upstream feature request makes: they belong in a contract the framework owns, so that a workload can be written once and every runtime can declare what it truthfully is. Until such a contract exists, this suite is one answer to that, deliberately a small one, and shaped so the others are backends beneath it rather than alternatives beside it.

## What the suite is

**A backend-neutral protocol that lets an agent's tool run its work in a sandbox — any sandbox — while the host decides which sandboxes qualify.**

Those layers are not so much competitors as an unfinished sentence. The boundaries exist, and several of them are very good; what is missing is a contract that lets an agent's workload be written **once** and run behind **any** of them, with the *host* deciding which ones are acceptable. That contract is what this suite is. It contributes no isolation of its own. It makes the isolation that already exists interchangeable, declarable, and accountable to the deployment rather than to the vendor.

The scattered landscape collapses into one shape. An ACA Sandbox, a Docker container, a container on a developer's laptop, an in-process fake — and, in principle, a containment system like MXC or a service like E2B — is each **one backend class**. A unit of work, whether that is validating a template, running a model-written program or rendering a diagram, is **one kind**, written against the protocol alone and never against a vendor. Between them sits a router, and the host tells it what it is willing to accept. When the backend changes, nothing else in the picture does.

Drawn, that shape is an hourglass.

![An hourglass, read left to right. On the left three chips are the kinds — a unit of work, written once: bicep_validate, execute_code and an open dashed one for a kind you write, each reached as an ordinary tool call; they narrow into one slate box, the contract, holding a spec for what the work needs and a router for what the host accepts. From there the shape widens again into the backends — ACA Sandboxes at micro-VM, Docker and wslc at container, an in-process fake for tests, and an open dashed chip for any sandbox — each named beside the boundary it declares. One line under the shape states what the arrangement buys: when the backend changes, nothing else in the picture does.](assets/suite-shape.svg)

And it is reached, always, as an **ordinary tool call** — the second half of the design and the half that is easy to get wrong. The *work* ships out to the sandbox; the *call* stays in the host process, where the middleware chain still sees it, still gates it, and still classifies what comes back. The tempting alternative, exposing the sandbox as a remote *agent*, leaves that security context altogether, and everything it returns has to be re-treated as untrusted ingress. Tool rather than agent is a security decision, not an ergonomic one.

### Why this rather than one of the alternatives

**You stop picking a sandbox.** The choice a service forces on day one — which vendor, which isolation technology, which region — becomes a configuration line you can change later, and a decision you can make differently in production, in CI and on a laptop. The same `bicep_validate` runs in a micro-VM, in a Docker container on a CI runner and in an in-process fake in a unit test, unchanged, because kinds and backends never import each other and tests fail if they begin to.

**The host owns the boundary decision, and can enforce it.** No sandbox service can tell you whether its boundary is strong enough for your deployment, because that is a question about your threat model and not about their product. Here the host declares a minimum isolation floor and a workload declares what it needs; a backend that sits below the floor, cannot confine egress to the hosts the workload named, or lacks a required capability is **refused** — at construction where possible — rather than quietly serving the request with less containment than was asked for.

**Your governance keeps working.** Because the sandbox stays on the tool path, MAF's approvals, budgets, telemetry and information-flow policy apply to it exactly as they apply to every other tool, with nothing to re-implement and no second security context to reason about. A suite that made you choose between isolation and governance would be solving one problem by creating another.

**It is shaped like the framework rather than like a session API.** A sandbox is keyed from the host's request context, so one conversation cannot address another's; it is reused warm across a fix-and-retry loop; it is purged from the service by label when the thread is deleted, which is correct even on the replica that never created it; and when nothing is configured, no tool is attached at all. Those are the details that go wrong quietly in a hand-rolled integration, and they are invariants here rather than advice.

**It is small enough to read.** The protocol is standard library only, one module in it touches `agent_framework`, and a new backend is a single class. There is no service to run, no daemon, no control plane, and nothing that has to be adopted wholesale — a host can attach one tool to one agent and stop there.

## What the suite is not

**Not an isolation technology.** It competes with no container runtime and no hypervisor; it is the seam and the policy above them. What it adds is making isolation *declarable*, *matchable against what a workload asked for*, and *enforceable before anything runs*. The analogy that fits is a database API and its drivers — the value is the contract, and that everyone on either side of it can be replaced.

**Not a sandbox provider.** Nothing here provisions infrastructure, builds an image or runs a service. A host brings a backend, and building and publishing the image a workload runs in is a deployment concern that stays outside the protocol on purpose.

**Not a replacement for the in-process runtimes.** If a workload fits an embedded interpreter or an in-process micro-VM, that is faster, cheaper and simpler than any remote sandbox, and it keeps host tool callbacks that remote services mostly cannot offer. The protocol's ambition is to have room for both ends of that range — a shared vocabulary in which an interpreter and a remote micro-VM can each declare what they truthfully are — rather than to argue that one of them wins.

**Not a file-writing library.** A kind can bring artifacts back out of a sandbox, but the landing callback belongs to the host, and the library never writes to the host filesystem itself. A kind that wrote where the agent's own file tools write would have handed model-authored bytes an approval gate they never passed.

**Not a guarantee about a backend you supply.** The router enforces what a backend *declares* against what a workload *asked for*. Whether a backend's declaration is true of the system underneath it is that backend's claim to make and its documentation's to justify — the protocol makes the claim explicit and checkable, which is a different thing from making it true.

## How it attaches to a MAF agent

Three roles, and the separation between them is what everything above rests on:

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

- **The protocol and the router** — `maf-sandbox`. The vocabulary (`SandboxKey`, `SandboxSpec`, `Sandbox`, `SandboxBackend`, `Isolation`, `Capability`) and the policy deciding whether a given backend may serve a given request. Its protocol modules import nothing but the standard library: a seam that depends on the things it separates is not a seam.
- **A backend** — *where* code runs. Azure Container Apps Sandboxes, a plain Docker container, a `wslc` container on a developer's own machine, or the in-process fake in `maf_sandbox.testing`. A backend declares what it can and cannot provide, and is held to it.
- **A kind** — *what work* runs. `bicep_validate` compiles agent-authored Bicep; `execute_code` runs a short program the model wrote and returns what it printed. A kind is a MAF tool with a sandbox behind it, written against the protocol alone.

What that looks like in one turn — note that exactly two things cross the process boundary, and the call is not one of them:

![The host application holds the agent, the middleware chain, the tool and the router; the code the agent wrote is untrusted. The host's own edge is the process boundary. Two arrows cross it — files in, to the sandbox, and result out, back again. The sandbox holds a work dir, the running code and a declared output, and has one egress valve, closed unless allowed.](assets/sandbox-flow.svg)

Exactly one module imports `agent_framework`, and it is deliberately not re-exported from the package: `import maf_sandbox` stays cheap and framework-free for a backend author or a workload's own test suite, while a host reaches the conveniences by name. Both halves are pinned by tests.

A host wires three things.

**The tools.** A kind's factory returns a plain list of MAF tools, which go onto the agent like any others:

```python
router = SandboxRouter([AcasSandboxBackend(config)])
context = make_caller_context(list_all_files, lambda: scope, lambda: thread_id)
tools = make_bicep_tools(router, store, agent_dir, context, image=image)

agent = Agent(client=client, name=agent_dir, instructions=..., tools=tools)
```

**The request context.** `make_caller_context` takes *callables*, read per call, rather than values. A sandbox is keyed by `(scope, thread_id, agent_dir)` — and by `call_id` as well for a workload that runs one sandbox per call, and a host that builds one agent and serves many conversations with it would — if the scope and thread were captured at construction time — let one conversation address another conversation's sandbox. Nothing in this stack accepts a scope, a thread id or a file path from the model: the file store listing is the boundary that decides what a name is allowed to resolve to.

`list_all_files` above is `maf_sandbox.maf`'s — it walks the store's `list_children` one level at a time and answers store-relative paths. It sits beside `make_caller_context` rather than in core because it reads `FileStoreEntry.type`, which is the framework's. A workload with no file channel at all passes `list_no_files`, which is a stated decision rather than an empty lambda the next reader has to interpret. Either way a failure to enumerate **propagates**: answering an empty list would read as "the store has no files" and refuse every name for the wrong reason.

**Disposal.** `SandboxPurger` participates in thread deletion, so deleting a conversation takes its sandboxes with it, and `dispose_scope` deletes by service-side label — which reclaims sandboxes the calling replica never created. A host that serves one conversation at a time can let `router.scope(scope, thread_id)` make that call for it: an async context manager that disposes however its block ends, and afterwards reports the count and — when the delete did not land — the reason, because zero reclaimed reads the same whether there was nothing to reclaim or nothing worked. One disposal is the framework's own: a sandbox it could not clean after a call — a removal that failed, or a stop that did not reach the program's whole process group — is disposed before the next call can reuse it, and the host loosens that on the router, never a kind ([`tool-call.md`](tool-call.md) § Cleanup).

Two behaviours are worth knowing before the first call, because both are deliberate and neither is what a host would arrive at by accident:

- **Nothing configured attaches nothing.** With no backend, a kind's factory returns `[]` — not a tool that fails when the model calls it. The host keeps its ungrounded behaviour with no half-attached error path, and the model is never shown a capability it does not have.
- **A backend that cannot honour the spec raises.** That is a misconfiguration rather than a choice, and a quiet degrade would ship a workload with containment it does not have. The distinction is the rule: absence is silent, dishonesty is loud.

## The guarantees, in detail

**Truth in place of plausibility.** The agent stops reporting that a template looks valid and starts reporting what the compiler said. Everything else here is what it costs to get that safely.

**Isolation declared rather than hoped for.** A host sets a minimum isolation floor on an ordered ladder — `none`, `runtime`, `os_process`, `container`, `hardened_container`, `microvm`, `vm` — and a backend below it is refused *at construction*, not at the first tool call, so a misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe. The default is `microvm`, the production posture; a workload's spec may raise the floor and can never lower it.

![Four backends ordered by increasing isolation and drawn with increasing border thickness: in-process fake, local container, container, cloud micro-VM. A vertical line marks the host's minimum isolation floor; the two weaker backends sit below it and are refused, the two stronger ones are admitted.](assets/isolation-floor.svg)

**Capabilities that mean something.** A backend declares what it can do — execute a command, take files in, read a declared file back out, enumerate a directory, and more — and the router matches that against what the workload asked for, refusing rather than degrading. One rule keeps the list honest: *name the backend that lacks it; if none does, it is a comment, not a capability.* That is why reading a declared output and listing a directory are separate capabilities, since Docker has no engine-level listing primitive — and why widening a write to accept bytes got no capability at all.

**Egress closed unless opened.** A spec names the hosts its work may reach, and a backend that cannot confine egress to that list is refused. A spec silent about egress gets the closed configuration, never the open one.

**Failures that do not leak.** When a sandbox is unavailable the model receives a fixed sentence saying the run degraded, and nothing else. The provider's own message — endpoint, subscription, tenant — goes to the log instead, because a tool result is persisted into the transcript.

## Where to read next

- [`samples/`](../../samples/) — the `app` box above, wired end to end, each small enough to read in one sitting.
- [`architecture.md`](architecture.md) — the layering, the vocabulary, keying and lifecycle, and the two directions a call crosses the boundary in.
- [`tool-call.md`](tool-call.md) — the life of one call: binding, call and run, what each owns, and the reclaim when it ends.
- [`policy-isolation.md`](policy-isolation.md) — the isolation ladder, the host's floor, and where known systems sit on it.
- [`capabilities.md`](capabilities.md) — what a sandbox can do, how it is declared and matched, and the whole file surface.
- [`network.md`](network.md) — the egress axis: what a workload may reach, and what a backend has to enforce before it may say so.
- [`hosts.md`](hosts.md) — the host boundary: where artifacts land, host tools called outward, identity, and the storage base.
- [`guest-platform-and-commands.md`](guest-platform-and-commands.md) — the guest-platform axis: what a kind may assume about the far side of the boundary, and how a backend finds out.
- [`kinds/README.md`](kinds/README.md) — what a kind is, what it owes the protocol, and the two that ship.
- [`backends/README.md`](backends/README.md) — the shipped backends side by side, and what each one honestly declares.
- [`backends/writing-a-backend.md`](backends/writing-a-backend.md) — the ordered path for a new backend author: what each `Sandbox` method owes, what to reach for, what never to do, and the probes that prove it.
- [`research/`](research/) — the records: the explorations and proposals the decisions came out of, kept in the tense they were written; how a new one graduates is [`../AUTHORING.md`](../AUTHORING.md).
- Each package's own README, for its configuration, its guarantees and its limits.
- [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) — the upstream feature request this suite is the reference implementation of.
