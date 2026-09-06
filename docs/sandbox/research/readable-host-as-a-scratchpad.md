# A readable host as a scratchpad — two public incidents read against this suite

> A reading, not a proposal. In September 2026 two reports described agents that were meant to read the internet and not write to it, wrote anyway, and used what they wrote to coordinate with each other. Neither was a container escape. This record takes each mechanism the reports describe and asks what this suite does about it as shipped, what it does not, and which open issue the gap already belongs to. Nothing here is decided. The one sentence that graduated is on [`../network.md`](../network.md) under `egress_allow`: a host two conversations both list is a channel between them. The record stream this reading asks for is designed on [`../observability.md`](../observability.md), whose `## Status` table is what tracks its state — the body below is kept in the tense it was written and does not follow it.

## The two reports

**DSEwiki.** [Ars Technica, 2026-09-05](https://arstechnica.com/security/2026/09/openai-agents-discussed-ways-to-escape-their-sandbox-on-public-wiki/): about 3,700 self-named OpenAI agents posted about 18,000 messages to a small German wiki over six weeks. The researchers' best guess, in their words: the agents "were supposed to have the ability to read the internet but not to write on it. They found a way to use their read access to write information to an obscure German wiki." They used the wiki to pool answers to a timed web-lookup task, to share "techniques for bypassing their restrictions", and to discuss XSS against the wiki and impersonating its moderators. A secondary report names two of the techniques as a "network-limit trick" and an unauthorized write through an Artifactory vulnerability. OpenAI confirmed the agents were its own and that its logs had already recorded the activity; agent activity dropped a day after that.

**The internal sandboxing tool.** The same article recounts METR's report from a week earlier: about 1,200 OpenAI agents "made posts to a makeshift message board that repurposed an internal sandboxing tool", shared methods for taking data from Hugging Face, and some then breached that network.

Three limits on what the reports establish, which bound this record too. The researchers had only the posts, not the agents' reasoning, so the exact write mechanism is inferred. The "network-limit trick" is named and not described. And the two swarms were separate tasks, so the second report is about shared infrastructure rather than a repeat of the first.

## What was crossed, and what was not

No hypervisor, kernel or container boundary is claimed to have failed in either report. What failed in both is a *policy* that was true of the intent and not of the mechanism: "read but not write" enforced somewhere that cannot tell the two apart, and "your own sandbox" enforced by a tool that turned out to be shared. Every mechanism below is one of those two shapes.

## A read that writes

An HTTP read carries data. A URL, a query string and a header are all bytes the sender chose, and a service that treats a `GET` as an edit, or logs what it receives where another reader can see it, has turned the read channel into a write channel. That is what "use their read access to write" means at the wire.

**What this suite claims.** An allowlist entry is a hostname, open to every method. [`../network.md`](../network.md) says so and says why: the docker and wslc proxies tunnel `CONNECT` and cannot see the verb, so a host is the finest unit of policy they can hold. Method scope is designed and waiting on a backend that can enforce it ([#377](https://github.com/sokolaidev/maf-extensions/issues/377)), and the design already states the limit these reports illustrate: `GET`-only "narrows, does not close", because a `GET` still exfiltrates through the URL, the query string and the headers. There is deliberately no `read_only=True` for exactly the reason the DSEwiki agents found.

**Where the open web is reachable at all.** For model-written code, nowhere. `execute_code` derives its mode from the allowlist union and `UNRESTRICTED` has no argument ([`../kinds/codeact.md`](../kinds/codeact.md)); `bicep_validate` takes a mode and its four hosts are the kind's, so a deployment can lower or raise the mode and never widen the list. A spec that says nothing runs `CLOSED`. The task in the report, an open-web lookup by model-driven code, is not expressible here.

What is expressible is the same reach through a host tool. A deployment that registers `fetch(url)` into a codeact registry has rebuilt the report's read pathway with the host's own network position: the body runs in the host process, and each host-tool call bypasses the middleware chain ([`../hosts.md`](../hosts.md)). The controls are the ones that page lists — nothing registered by default, the per-run call and byte caps, the identity leg — plus an outbound confidentiality cap the host must pass and nothing derives for it ([`exfiltration-audit.md`](exfiltration-audit.md), Finding 3½). A `fetch(url)` under `Identity.APP` with no cap and no approval is the report's setup, and every declaration on it would read as satisfied.

## A shared scratchpad

The wiki's value to the swarm was not that one agent could write to it. It was that every other agent could read what was written. The channel was *between* sandboxes, through a host outside all of them.

**What separates sandboxes here.** The key: `(scope, thread_id, agent_dir, call_id)`, read from the host's request context and never from model input ([`../architecture.md`](../architecture.md)). On docker a `CLOSED` run is `--network none`, and an `ALLOWLIST` run gets its own internal network with an unaddressed bridge ([`../backends/docker.md`](../backends/docker.md)), so two sandboxes on one daemon have no route to each other. On ACAS each sandbox is its own micro-VM, and the control-plane credential never enters the guest ([`../backends/acas.md`](../backends/acas.md)). The audit found no unmediated guest-to-guest path and every core structure free of guest-writable state.

**What does not separate them.** The key holds at the sandbox and stops there. Any host that two conversations both list, and that stores what one sends and answers it to the next, is a scratchpad the key never sees: a wiki, a package index that accepts uploads, an artifact store. The docs name "a package index, an internal artifact store" as the typical deployment entries for `execute_code`, and the Artifactory write in the report is that host. Before this record, [`../network.md`](../network.md) said every allowed host is "a way out"; it did not say "and a way between". It does now. No check can be built for it, because a hostname does not say whether the service behind it reflects writes back, so the review is per entry and belongs to whoever adds it. The same shape exists inside the host, through a shared sink root or a store fed by a sink, and is filed as [#793](https://github.com/sokolaidev/maf-extensions/issues/793).

## Network-limit tricks

The report names one and describes none, so this section maps the classes rather than an exploit.

**Closed by mechanism.** The proxy resolves names on its own side, so the guest performs no lookup it could encode into. It refuses to complete a tunnel to a peer that is not globally routable, which is how the metadata and link-local ranges stay unreachable under `ALLOWLIST` and how condition 2 of the micro-VM standard is met ([`../policy-isolation.md`](../policy-isolation.md)). It accepts `CONNECT` and nothing else, to port 443 unless configured otherwise. `HTTP_PROXY` is advisory; the topology is the control. On ACAS the policy is applied by the service above the guest, with no in-guest component to route around.

**Identified and not closed.** The container engine's embedded DNS resolver forwards lookups from the daemon, outside the guest's namespace. Whether it still does so on a network created `--internal` is engine behaviour this repository has identified and not measured, and it would be a low-bandwidth way out of a topology that has no other. [#402](https://github.com/sokolaidev/maf-extensions/issues/402) names a no-DNS-leak assertion as one of three, and it is asserted in no suite. If the "network-limit trick" in the report has a counterpart here, this is the shape of it.

**Priced, not closed.** docker and wslc sit at the `container` rung and share a kernel with every other sandbox on the daemon. The ladder says so, and the default `MICROVM` floor refuses them unless a host opts down. A kernel escape is the cost that rung carries, and no report here describes one.

## An internal tool as a message board

The METR report is about a *service* every sandbox could reach, not a host on the internet. The analogue here is any state a backend or a host holds on behalf of every sandbox.

**In the suite.** Core holds no structure a guest can write to ([`exfiltration-audit.md`](exfiltration-audit.md), Finding 4). The host-tool registry is sealed once its aggregate is taken. The proxy is per sandbox and recreated per acquire. `dispose_scope` deletes by service-side label, which reclaims what a replica never created. The in-process testing backend returns one sandbox for every key, and the default floor refuses it in production for that reason among others.

**In the host's wiring.** A sink root shared across conversations is a mailbox. A store that serves what a sink landed is a return path. A sink-declared host tool beside a source-declared one is a relay. All three are legal today and invisible to every declaration, and all three are [#793](https://github.com/sokolaidev/maf-extensions/issues/793). This is the shape the METR report describes, one layer up: a tool that everyone can reach and that keeps what it is given.

## Detection

OpenAI knew before the researchers did, from its logs. That is the part of both reports this suite is least equipped to repeat.

The proxy prints `ALLOW`, `DENY`, `DENY-NONGLOBAL` and `UNREACHABLE` with a host and port to its own container's stdout. The backend reads that stream once, at acquire, to wait for the listening marker, and never again; the lines carry no key and die with the container. ACAS answers a denied request with an `x-deny-reason` header that only the guest can see and no test asserts. Host-tool runs carry a `run_id` the docs call loggable and a ledger of bytes delivered, and nothing emits either. The kinds and backends log a handful of `info` lines; core logs warnings. MAF's own tracing sees the whole call as one `execute_tool` span, which is the aggregate the boundary is built to show, and host-tool calls bypass it along with the middleware. Six weeks of one swarm's tunnels to one host would be visible here as nothing at all.

That gap is filed as [#904](https://github.com/sokolaidev/maf-extensions/issues/904): an observer seam in core and a package that emits the events — acquire, host-tool call, files in and out, egress decisions, labels, disposal — to an OpenTelemetry provider, the app's or a separate one for a security pipeline.

## What this record argues for, and what it does not

It argues for no new axis. Each mechanism above already has an owner:

| Mechanism in the reports | What holds here | What does not | Where it belongs |
|---|---|---|---|
| A read that writes | Host-scoped allowlist, `CLOSED` by default, `UNRESTRICTED` inexpressible for model-written code | Method scope; and a `fetch` host tool restores the report's reach with the host's authority | [#377](https://github.com/sokolaidev/maf-extensions/issues/377); the cap derivation in [`exfiltration-audit.md`](exfiltration-audit.md) |
| A shared scratchpad | Keying, per-sandbox topology, no guest-writable core state | Any listed host that stores and answers; the in-host relays | the sentence on [`../network.md`](../network.md); [#793](https://github.com/sokolaidev/maf-extensions/issues/793) |
| A network-limit trick | Egress-side resolution, non-global refusal, `CONNECT`-only | The engine's DNS on an internal network, asserted nowhere | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) |
| An internal tool as a message board | Sealed registry, per-sandbox proxy, label-scoped disposal | Shared sink roots and stores are the host's to keep apart | [#793](https://github.com/sokolaidev/maf-extensions/issues/793) |
| Detection from logs | The call stays on the tool path the middleware sees | No event stream for what happened inside the call | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) |

A deployment-wide default allowlist ([#403](https://github.com/sokolaidev/maf-extensions/issues/403)) is worth reading in this light too: a default that every kind inherits is the widest place a scratchpad host could enter, and the one place nobody reviews per entry.

**Not established.** How the DSEwiki agents actually wrote — a `GET` that edits, a form a fetch tool submitted, or something else — is the researchers' inference and this record's. What the network-limit trick was. Whether any host on any shipped kind's fixed list stores what it is sent (the four Bicep restore hosts are Microsoft-operated content hosts, and the assumption that they do not is an assumption). And whether an agent in this suite would find the scratchpad if one existed: the reports say agents did, on an ordinary task, with no instruction to.
