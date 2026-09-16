# Egress research

> Consolidated research record for the egress mode model and method-scoped allow entries, measured and decided across 2025–2026. The resolved operational contract lives in [`../network.md`](../network.md); this record keeps the design arguments, migration evidence, backend boundaries and live-measurement limits without repeating the full network guide.

## Decision at a glance

Egress is a workload mode, not a backend property to approximate. A workload declares exactly one of `UNRESTRICTED`, `ALLOWLIST` or `CLOSED`; a kind validates which modes it can accept; a backend declares the set of modes it can enforce. The router serves the exact requested mode only when it is a member of the backend's set. Otherwise it refuses at attach and acquisition with `SandboxEgressNotEnforced`.

The default is `CLOSED`. `egress_allow` is the payload of an `ALLOWLIST` run and is ignored for the other modes. A non-empty host list with `CLOSED` or `UNRESTRICTED` is invalid at spec construction. No backend silently substitutes a more-open or more-isolated mode, and no warning path turns a mismatch into a successful tool attachment.

`Capability.NETWORK` and the old `Egress.UNDEFINED` member were removed. Whether a workload needs network access depends on its deployment and inputs, so it belongs in the per-spec mode rather than in a fixed kind capability. An undeclared backend is represented by an empty enforceable mode set and is refused every egress ask.

Method-scoped allow entries are an additive refinement of `ALLOWLIST`, not a fourth mode. `EgressRule(host, methods)` narrows one host to uppercase HTTP-token methods and derives `Capability.EGRESS_METHODS`. No shipped backend currently declares that capability; Docker and WSLC use CONNECT tunnels and are host-only, Hyperlight withholds it, and ACAS refuses method-scoped rules despite live evidence that its HTTPS service can deny unmatched methods.

## The resolved mode model

### Three declarations

| Owner | Declaration | Meaning |
|---|---|---|
| Workload/deployment | `SandboxSpec.egress` | The one posture this run will use; defaults to `CLOSED` |
| Kind factory | Accepted mode set | The postures this workload can honestly operate in; checked during construction |
| Backend | `egress_modes: frozenset[Egress]` | The postures the configured enforcement mechanism can actually deliver |

`egress_allow` remains a tuple of host entries for `ALLOWLIST`. It is not a deny-list and does not describe an implicit open world. Every host omitted from the tuple is denied. A deployment adding a host widens what model-authored code or a compiler can send to that host, including any data the sandbox can read; a shared host can also become a cross-conversation scratchpad, so the host author owns that risk.

Resolution is one membership check:

```text
serve iff spec.egress in backend.egress_modes
otherwise refuse
```

The router runs it both in `ensure_can_serve` and `acquire`. A caller skipping attach preflight receives the same refusal. The kind's accepted set has already been spent at construction and does not re-enter router resolution.

`UNRESTRICTED < ALLOWLIST < CLOSED` is explanatory prose, not a rank used for fallback. Egress never searches for the “closest” posture. `CLOSED` is the most isolated mode and the fail-closed default; `UNRESTRICTED` is an explicit opt-in for a workload and backend that accept no network confinement.

### Why the old match-and-warn model failed

The earlier model compared a single backend declaration against `egress_allow` and allowed a backend to confine more than requested with a warning. Three cases exposed the problem:

1. Bicep may list module hosts while a module-free template works completely offline. Naming hosts does not mean the current input requires them; the deployment can select `CLOSED`.
2. The in-process Bicep sample cannot confine egress. Requiring it to claim `CLOSED` made the honest `UNRESTRICTED` declaration unusable. It now runs only when the host explicitly lowers the isolation floor and the workload explicitly asks for `UNRESTRICTED`.
3. `Capability.NETWORK` had no stable meaning: no kind or backend could answer the fixed yes/no question “does this workload need network?” The mode depends on the concrete deployment and input.

Serving a `CLOSED` sandbox for an `ALLOWLIST` ask is not a successful safer result. It changes the workload's contract and can turn a direct wiring error into a late module-restore traceback. Serving `UNRESTRICTED` for an allowlist silently widens reach. Both directions are therefore refusal cases.

### Kind examples

Bicep accepts all three modes. Its factory defaults to `ALLOWLIST` with its fixed AVM restore hosts, but a module-free validation can select `CLOSED`, while the explicitly unconfined in-process sample selects `UNRESTRICTED`. The kind's fixed host set is not widened by a deployment; only the mode changes.

CodeAct accepts `CLOSED` and `ALLOWLIST`, never `UNRESTRICTED`. Model-written code reaching anything is the exfiltration case the allowlist is meant to constrain. An empty effective allowlist produces `CLOSED`; a non-empty one produces `ALLOWLIST`. The kind's own required egress is fixed in code, while deployment hosts add their separately authorized destinations.

A render-only kind can accept only `CLOSED`. A kind that cannot function offline omits `CLOSED` and is refused on a backend that offers only closed egress. This guard prevents an apparently valid attachment that can never initialize its workload.

## Payload grammar and ownership

Core validates every allow entry because all kinds ultimately feed the same backend surfaces. An entry is one schemeless hostname: DNS labels of letters, digits and hyphens, each within the allowed length, optionally with one leading `*.` wildcard. Schemes, ports, paths, whitespace, commas, trailing dots, glob characters and a bare `*` are refused. A bare string passed instead of a sequence is refused rather than becoming one host per character. A bare `*` is rejected by name because it is effectively `UNRESTRICTED` disguised as an allowlist.

The kind's fixed needs and the deployment's additions form the effective union. A deployment cannot widen what the kind itself requires by replacing its fixed list, but it can select the mode appropriate to its concrete input. CodeAct renders the effective hosts to the model; the backend enforces them. A host author must treat every entry as an actual outbound channel, not as documentation.

A host cannot express the security properties of the service behind its name. The egress mode bounds where the sandbox may send; it does not prove that the service is read-only, that it will not echo data to another user or that a package feed is trustworthy. Those remain application and information-flow concerns.

## Backend enforcement

| Backend | Enforceable modes | Enforcement mechanism |
|---|---|---|
| ACAS | `ALLOWLIST`, `CLOSED` | Azure service `EgressPolicy`: deny by default with explicit host allow rules |
| Docker | `CLOSED`, plus `ALLOWLIST` when a proxy image is configured | `--network none`, or an internal network with an unaddressed bridge and dual-homed CONNECT proxy |
| WSLC | `CLOSED`, plus `ALLOWLIST` when configured | Internal network plus dual-homed CONNECT proxy; same proxy source as Docker |
| In-process sample backend | `UNRESTRICTED` | No confinement; honest only when the workload explicitly accepts it |
| Hyperlight | No method-scoped declaration; host-wide allowlist support is separate | Native guest HTTP policy, with method enforcement not yet declared by this suite |

Docker/WSLC proxy variables are advisory. Topology is the enforcement: the workload network has no other route out. Host firewall/iptables rules are not portable across rootless engines or Docker Desktop/Colima/OrbStack/Rancher Desktop VMs. ACAS enforces at its service/L7 policy boundary. Hyperlight's native wasi-http boundary can see methods, but its backend declaration remains conservative until its complete surface is qualified.

The micro-VM isolation standard treats egress as one of four required claims: a backend claiming `MICROVM` must enforce `ALLOWLIST` or `CLOSED`, not only `UNRESTRICTED`. This egress check is independent of the isolation floor: isolation decides whether the boundary is strong enough, while the resolved mode decides how that boundary is configured.

## Method-scoped allow entries

`EgressRule(host, methods)` is a frozen public policy value. A bare host means all methods; a rule with a non-empty method tuple narrows that host. `methods=None` can be constructed as an all-method rule and is canonicalized to a bare host in `SandboxSpec`; empty method tuples are invalid. Core rejects malformed HTTP tokens and duplicate methods. Hosts and method sets are normalized for conflict detection without rewriting the author's surviving spelling/order.

Equivalent entries collapse. Duplicate bare hosts, case variants of a bare host and method sets with the same members in another order are one policy. A bare host beside a narrowed rule, or two different method sets for one host, conflicts and raises rather than guessing which should win. A scheme-qualified host is rejected by kind validation; a method rule does not bypass the existing host grammar.

The spec derives `Capability.EGRESS_METHODS` whenever a method-scoped entry survives canonicalization. The capability is absent from `DEFAULT_CAPABILITIES`. A backend declaring it publishes `egress_method_tokens`; a finite set means those exact tokens, while `None` means every core-valid token. Router preflight, per-spec routing and acquire all require both the capability and token membership. A backend-local check alone is insufficient because attach and acquire could disagree.

Method scope narrows but does not close egress. GET can exfiltrate in URLs, query strings, headers and request bodies. There is no `read_only=True` shorthand, and method policy does not remove the outbound result from information-flow review.

### Live ACAS evidence

The ACAS measurement used `azure-containerapps-sandbox` 0.1.0b4, API `2026-02-01-preview`, an HTTPS origin accepting arbitrary method tokens and full traffic inspection with deny-default policy.

| Policy | Request methods | Result |
|---|---|---|
| All methods | GET, POST, `get`, `Get`, PROPFIND, `propfind`, `PropFind`, X-CUSTOM | 200 |
| GET only | GET, `get`, `Get` | 200 |
| GET only | POST, PROPFIND, `propfind`, `PropFind`, X-CUSTOM | 403 with service denial reason |
| `get` only | GET, `get`, `Get` | 200 |
| PROPFIND only | PROPFIND, `propfind`, `PropFind` | 200 |
| PROPFIND only | GET, POST, `get`, `Get`, X-CUSTOM | 403 |

The service admits custom methods but matches case-insensitively. A GET request carrying synthetic content reached the origin under GET-only policy, proving method scope is not body-free or read-only. The normal GET/POST probe would pass, but redirects, rule precedence, wildcard overlap, every token, and non-TLS behavior remain unmeasured. ACAS therefore refuses method-scoped rules and withholds `EGRESS_METHODS`.

### Conformance

Method enforcement requires two pre-acquired subjects: a control with all methods allowed and a scoped subject with GET only. The endpoint must accept both GET and POST under the control policy, GET must reach under the scoped policy, and POST must be refused under it. Otherwise an endpoint returning 405 could make a non-enforcing backend appear correct. Runtime backends implement the request seam through their own guest API; the shared suite does not assume a shell or curl. Acquisition and disposal remain backend wiring responsibilities.

The method probe intentionally does not require lowercase `get` to reach under `GET`. A stricter case-sensitive backend would admit a subset of the contract, and the conformance suite must not reject stronger enforcement for a spelling guarantee the final contract does not make. Core accepts uppercase method tokens; service matching behavior is backend evidence, not a promise of literal case semantics.

## Evidence and rollout

The resolved-mode decision shipped through the core egress set and router membership work: the old mismatch warning path was removed, `UNDEFINED` was removed, and `NETWORK` was removed. Bicep and CodeAct factories validate their accepted modes before a spec reaches the router. Samples 05 and 09 demonstrate the two important cases: a module-free Bicep workload served closed by Docker, and an explicitly unconfined in-process Bicep workload served unrestricted after the host lowers the isolation floor.

Method scope is additive and was designed to land in three stages: widen the core payload and add `EgressRule`; release the core changes; then adopt the widened type and `EGRESS_METHODS` in declaring kinds/backends behind the released core version. Existing consumers must render union entries safely before the core field widens. No backend should claim enforcement until its API boundary, token set, redirects, precedence, wildcard behavior and conformance probes are qualified.

The shared egress outcome probes run from inside the guest with an allowed-host positive control and a denied-host negative control. Docker runs live tests after merge, daily and on demand; ACAS runs against the real service in scheduled/manual verification; WSLC requires its Windows/WSL environment. The shared result measures the outcome, while backend-specific tests may additionally assert a mechanism such as Docker's `000` network refusal or ACAS's denial header.

## Remaining limits

- A deployment-wide default allowlist remains open work; each spec currently chooses its mode explicitly.
- Method-scoped enforcement is not shipped by any backend. ACAS has partial live evidence but refuses the capability; Docker and WSLC are host-only CONNECT proxies; Hyperlight has a method-aware native primitive but no complete backend claim.
- Egress does not bound attached identity, host-tool calls, output sinks, inbound listeners or service-side data retention. Those channels have separate contracts.
- Internal DNS forwarding behavior on container engines, redirects, wildcard precedence, non-TLS requests and service-specific policy precedence need independent measurements before stronger claims.
- `CLOSED` is a network posture, not proof that a guest cannot publish data through a host callback or that a result cannot contain untrusted instructions.
