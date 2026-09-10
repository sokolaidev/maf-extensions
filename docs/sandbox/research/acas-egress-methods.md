# ACAS method matching and policy reuse

> Live measurements for [#377](https://github.com/sokolaidev/maf-extensions/issues/377), taken on 2026-09-10 against the HTTPS service path. The contract and current backend behavior live in [network.md](../network.md#method-scoped-allow-entries) and [acas.md](../backends/acas.md). This record explains why an SDK method field is insufficient to declare literal method enforcement.

## Setup

The baseline was main `7de3c5e1`, with `maf-sandbox-acas` source version 0.21.0, `azure-containerapps-sandbox` 0.1.0b4, API `2026-02-01-preview`, host Python 3.12 and guest Python 3.13. Azure CLI authentication located the existing validation group. An imported Python image served a temporary HTTP endpoint through the service's HTTPS ingress. It accepted arbitrary method tokens, returned 200, and recorded the received method and synthetic request content. The origin had deny-default egress; separate guest sandboxes received all-methods or method-scoped policies with `trafficInspection=Full`.

The SDK policy used `EgressPolicy.rules`, with `EgressRuleMatch(host=..., methods=[...])` and `EgressRuleAction(type="Allow")`. Scoped hosts had no unconditional host allow. Curl sent HTTP/1.1 with `-X` and recorded the outgoing request line. These traces distinguish literal guest requests from spellings a proxy rewrites. All-methods controls established that the endpoint accepted the tested methods. Service `x-deny-reason` headers identified policy denials; origin receipts identified arrivals. Only synthetic data was sent.

## Measurements

| Policy | Guest method | Response |
| --- | --- | --- |
| All methods | GET, POST, get, Get, PROPFIND, propfind, PropFind, X-CUSTOM | 200 |
| GET only | GET, get, Get | 200 |
| GET only | POST, PROPFIND, propfind, PropFind, X-CUSTOM | 403 with service denial reason |
| get only | GET, get, Get | 200 |
| PROPFIND only | PROPFIND, propfind, PropFind | 200 |
| PROPFIND only | GET, POST, get, Get, X-CUSTOM | 403 with service denial reason |

The outgoing trace retained `get` and `Get`; their origin receipts contained GET. That alone cannot attribute the rewrite to one proxy because the path includes service egress and ingress. The custom-method observation does not depend on that attribution: a PROPFIND-only policy admitted `propfind` and `PropFind`, and the origin recorded those exact spellings. The policy therefore admits more methods than core's case-sensitive rule names.

A GET request carrying `issue377-synthetic-body` arrived with its content under GET-only policy. Method filtering does not establish body absence or read-only behavior. URLs, headers and request content remain outbound channels, as core's `EgressRule` docstring states.

The ordinary GET/POST conformance controls would pass this service: unscoped POST reaches, scoped GET reaches, scoped POST is denied. They do not establish literal case semantics. Declaring only uppercase policy tokens would not repair the wider set of request spellings admitted by the service. `EGRESS_METHODS` must remain withheld under its present contract; a case-insensitive policy would need a separate, explicit design.

## Reuse

A second live measurement used the baseline `SandboxRouter` and `AcasSandboxBackend`. It acquired a host-wide allowlist, changed the same key/kind to another host, then changed it to CLOSED. Every acquire returned the same physical instance, and the original endpoint answered 200 after both changes. The service received no replacement policy. Adoption cleanup did not repair the mismatch because the router already knew the instance. This is a surviving-instance case; disposal between completed tool calls reduces exposure.

The implementation now records the mode and normalized host set with the held instance and refuses a mismatch before resume. It retains the original holder and cleanup ownership. A caller must dispose the kind or choose another key before changing policy. Automatic replacement was not chosen because another caller may still use the instance. The live regression verifies equivalent-policy reuse, mismatch refusal, continued availability under the original policy, and a correctly denying CLOSED replacement after explicit disposal.

## Limits and cleanup

These measurements establish the tested HTTPS path and tokens. They do not establish HTTP behavior, every token, redirects, wildcard overlap, rule precedence, alternative TLS/request paths or Hyperlight behavior. Those remain required evidence for any future enforcement claim. All probe-owned sandboxes, including the temporary origin, were deleted and their label-filtered inventory was verified empty.
