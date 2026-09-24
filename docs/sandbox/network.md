# Network policy

`SandboxSpec.egress` states the workload's required outbound network policy. The kind defines which policies it accepts. The backend declares which policies it can enforce. The router requires an exact match.

## Three modes

```
unrestricted  <  allowlist  <  closed
       increasing restriction on outbound destinations
```

| Mode | Contract |
|---|---|
| `UNRESTRICTED` | No outbound destination restriction is promised |
| `ALLOWLIST` | Only destinations in `egress_allow` are permitted |
| `CLOSED` | Outbound network access is denied |

The spec defaults to `CLOSED`. A nonempty `egress_allow` requires `ALLOWLIST`. An empty allowlist permits no destinations.

The router checks membership in `backend.declarations.egress_modes`. It does not replace the requested mode with a more open or more restrictive mode. An empty backend declaration refuses every workload with `SandboxEgressNotEnforced`.

![The host selects a workload policy accepted by the kind. The router checks that exact mode against the backend declaration, then the backend enforces it. Closed denies outbound destinations, allowlist admits only listed hosts, and unrestricted promises no destination restriction. Method and path rules additionally require EGRESS_METHODS and EGRESS_PATHS. Output sinks, host tools and attached authority have separate checks.](assets/network-policy.svg)

## Kind policies

| Kind | Accepted policy |
|---|---|
| Bicep | `ALLOWLIST` with its fixed compiler hosts by default; `CLOSED` for module-free use; explicit `UNRESTRICTED` for host testing |
| CodeAct | `ALLOWLIST` when the host supplies destinations; otherwise `CLOSED` |
| draw.io | `CLOSED` |
| Terraform | `CLOSED` |

Bicep's allowlist contains `mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms` and `live-data.bicep.azure.com`. The host cannot widen that fixed set through the factory. The [kind guides](kinds/README.md) explain the workload constraints.

## Host rules

`egress_allow` accepts host strings and `EgressRule` values. A host string allows all methods and paths for that host. A rule can restrict methods or paths, or request an attached-authority header.

Hostnames contain dot-separated labels of letters, digits and hyphens. Each label is at most 63 characters. A leading `*.` is the only wildcard form. Schemes, ports, paths, whitespace, commas, trailing dots and other wildcard forms are invalid.

Host matching is case-insensitive. A leading `*.` matches subdomains but not the bare name. Equivalent entries collapse while retaining the first spelling and rule order. Conflicting rules for one host are refused, including a host-wide rule paired with a narrower method, path or authority rule.

Pass a sequence of entries. A bare string is refused instead of being treated as a sequence of characters. Backend-specific limits can be stricter than the shared hostname grammar.

## Backend enforcement

| Backend | Mechanism and limits |
|---|---|
| ACAS | Service-enforced policy with default-deny allowlisting; no egress observations from the adapter |
| Docker | `CLOSED` through no-network mode; host, method and path allowlisting through configured iron-proxy and an isolated workload network |
| WSLC | No-network mode or configured iron-proxy; host, method and path rules with engine-specific limits |
| Hyperlight | Runtime HTTP permissions; closed or exact-host allowlisting, without wildcards or method rules |
| In-process fake | Test declarations only; no network containment |

ACAS refuses a warm sandbox requested with a different mode or normalized host set through `AcasEgressPolicyConflict`. Dispose it or use a different key. The conflict does not evict the original sandbox.

Docker and WSLC need proxy configuration before they can serve a nonempty allowlist. The workload has no direct external route through the configured topology. Proxy environment variables help clients find the proxy; those variables are not the boundary.

The packaged proxy is built from pinned iron-proxy source with a local policy patch. It checks the listed host before establishing a tunnel, then checks each HTTP method and path after terminating guest TLS. It validates upstream certificates and gives the guest a per-sandbox CA certificate; the signing key stays in the proxy. The proxy resolves each upstream dial and checks the selected IPv4 or IPv6 address. Listed hosts may resolve to private addresses. Loopback, link-local, metadata, inspected network gateways and the proxy's own interface addresses are denied. The proxy is recreated on acquisition rather than adopted from an earlier host process. Both adapters require its patched policy-contract signal and listening signal before serving a workload; an older image fails the acquire.

Public destinations require TLS on every port. Private destinations also require TLS unless the host explicitly sets `allow_private_http=True` on the Docker or WSLC config for development or test use. That option permits plaintext only when the listed host's actual resolved address is private. An HTTP redirect requires a new request through the same policy. Client libraries must use the injected proxy and CA environment or configure equivalent trust; the network topology blocks direct outbound routing.

An empty allowlist uses a network with no outbound destinations. It still answers the requested `ALLOWLIST` policy. A proxy that cannot establish its outbound connection causes refusal.

The [Docker](backends/docker.md) and [WSLC](backends/wslc.md) guides describe topology, DNS limits and tested behavior. In particular, do not infer complete DNS confinement from CONNECT enforcement. Egress observations also cover only the windows the backend can attribute; see [observability](observability.md).

<a id="method-scoped-allow-entries"></a>

## HTTP method restrictions

```python
from maf_sandbox import Egress, EgressRule, SandboxSpec

spec = SandboxSpec(
    kind="api-reader",
    egress=Egress.ALLOWLIST,
    egress_allow=(EgressRule("api.example.com", ("GET",)),),
)
```

`methods=None` permits all methods for the host. A nonempty tuple permits only the named uppercase tokens. `HttpMethod` provides common values; valid custom uppercase tokens are accepted. Empty, duplicate or malformed method sets are refused. Input is not silently uppercased.

Any method-limited rule adds `Capability.EGRESS_METHODS` to `required_capabilities`. The backend must declare that capability and permit every requested token through `egress_method_tokens`. A finite set permits those tokens; `None` permits every valid token; the default empty set permits none.

The contract restricts the verb, not a client's spelling convention. A backend cannot admit a different verb through case handling. The declaration does not promise whether a client spelling such as `get` is accepted.

Docker and WSLC advertise `EGRESS_METHODS` when their iron-proxy image is configured. The proxy terminates guest TLS to inspect the method. ACAS and Hyperlight withhold this capability.

An explicit `EGRESS_METHODS` requirement remains a requirement even if a later spec replacement removes method rules. `GET` still sends query text, headers and other data outward; it is not a confidentiality exemption or a read-only guarantee.

## HTTP path restrictions

`EgressRule("api.example.com", paths=("/v1/*",))` permits `/v1` and paths below `/v1/`. Other paths require an exact match. Rules may combine `methods` and `paths`; each constrained request must satisfy both. Queries are not part of the path rule. Empty, duplicate, relative, dot-segment and other glob patterns are refused.

Any path-limited rule adds `Capability.EGRESS_PATHS` to `required_capabilities`. Docker and WSLC advertise it with iron-proxy configured; other shipped backends refuse it. Path matching checks the HTTP request path. The destination can still interpret encoded characters or redirects differently, so a path rule is not a confidentiality guarantee.

## Authority on a destination

`EgressRule(..., authority=...)` names an exact token audience for an attached-authority header. The host must opt into `ATTACHED_IDENTITY`, its sharing scope and its retention bound. Wildcard authority destinations are refused.

A rule with authority and `methods=None` does not require `EGRESS_METHODS`. Header authority and HTTP methods are separate checks. [The host identity contract](hosts.md#identity--whose-authority-sandbox-work-carries) defines the complete admission rules.

Docker and WSLC implement this through a [host-configured credential gateway](hosts.md#credentials-for-guest-http-requests). Each grant adds an exact HTTPS origin and expiry to the rule. It cannot widen the rule's methods or paths. Public and private credential destinations both require verified TLS; the private HTTP development exception applies only to requests without gateway credentials.

## Verify enforcement

An allowlist conformance test needs both a permitted destination that responds successfully and a denied destination. Cutting off all networking is not proof of a working allowlist.

The default `EXEC` probe uses guest `curl`. A runtime backend supplies a probe through its own API. Method conformance checks an allowed GET, a denied POST and an unrestricted-method control where POST succeeds.

These probes test their stated cases. Redirects, alternate protocols, wildcard behavior and authority forwarding need evidence from the backend's own tests. Do not turn a passing small probe into a broader claim.

## Other ways data leaves

Network policy applies to the guest's outbound network. It does not bound artifact delivery, host-tool bodies running in the host, or platform authority channels. Each has a separate [host contract](hosts.md).

ACAS group-configured identity is supported outside the core attached-identity declaration. `CLOSED` alone does not establish that such an identity is absent. The host must choose a group with the intended authority.

An allowed service can store data and return it to another conversation. Separate sandbox keys cannot isolate that service's records. The host owns destination partitioning and source/sink policy.

Egress is not an ingress policy. It does not promise that guest code cannot listen on local interfaces or leave a process running when reuse is enabled. [Call isolation and cleanup](tool-call.md) govern sandbox lifetime.

## Status

| Decision | State | Tracking |
|---|---|---|
| Exact egress mode and host-rule matching | Implemented | [#34](https://github.com/sokolaidev/maf-extensions/issues/34) (closed); [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed); [#524](https://github.com/sokolaidev/maf-extensions/issues/524) (closed); [#534](https://github.com/sokolaidev/maf-extensions/pull/534) (merged); [#1126](https://github.com/sokolaidev/maf-extensions/issues/1126) (closed); [#1138](https://github.com/sokolaidev/maf-extensions/pull/1138) (merged) |
| Backend enforcement | Implemented with backend-specific limits | [Backend guides](backends/README.md) |
| Method and path rules | Core, Docker and WSLC implemented; other backend adoption remains open | [#377](https://github.com/sokolaidev/maf-extensions/issues/377) (open); [#1409](https://github.com/sokolaidev/maf-extensions/pull/1409) (merged) |
| IPv6 upstream addresses | Docker private HTTP/TLS and denials measured; WSLC denials measured, private IPv6 blocked by engine network support | [#1407](https://github.com/sokolaidev/maf-extensions/issues/1407) (open); [#1409](https://github.com/sokolaidev/maf-extensions/pull/1409) (merged) |
| Deployment default allowlists | Unimplemented | [#403](https://github.com/sokolaidev/maf-extensions/issues/403) (open) |
| Attached-authority destinations | Core admission and Docker/WSLC external gateways implemented | [Host identity status](hosts.md#status) |
| Egress conformance and observations | Implemented; evidence is limited to measured cases and attributable windows | [Backend conformance](backends/writing-a-backend.md); [observability](observability.md#status) |
