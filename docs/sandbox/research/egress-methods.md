# Method-scoped egress: a refinement of the allowlist payload, not a fourth mode

> The proposal that argued method scope (`GET`-only against a host) into the egress vocabulary — an `EgressRule` beside the plain hostname, a backend `Capability` that says whether it can enforce one, and a refusal that falls out of the capability match already in the router. Tracked by [#377](https://github.com/sokolaidev/maf-extensions/issues/377). It is kept in the tense it was written, as the record of the argument; the decided shape lives in [`../network.md`](../network.md). It builds on the resolved-mode model argued in [`egress-resolution.md`](egress-resolution.md) and lands with the first API-boundary backend, explored in [`hyperlight-backend-exploration.md`](hyperlight-backend-exploration.md).

---

## The gap

`SandboxSpec.egress_allow` is a tuple of hostnames, and `Egress` says how precisely a backend confines — `UNRESTRICTED`, `ALLOWLIST`, `CLOSED`. Neither can say *which HTTP methods* an allowlisted host is reachable by. A workload that should read an API and never write to it — grant `GET` and nothing else — has no way to ask for that, and a backend that could enforce it has no way to say so.

The refinement already exists one layer down. hyperlight-sandbox's `allow_domain(target, methods)` restricts an allowlist entry to named methods, enforced natively at the wasmtime-wasi-http boundary (read for [#371](https://github.com/sokolaidev/maf-extensions/issues/371); [`hyperlight-backend-exploration.md`](hyperlight-backend-exploration.md)). So the mechanism is real and shipping in a guest we intend to adopt; what is missing is the vocabulary for a spec to request it and for the router to refuse a backend that cannot deliver it.

## Why it is worth having, stated without overselling

`GET`-only against an API host drops the common write and upload verbs — `POST`, `PUT`, `PATCH`, `DELETE` — from the channel, so the routine ways of pushing bytes out are gone. It **narrows** the sink; it does not **close** it. A `GET` still exfiltrates through the URL, the query string and the headers, and HTTP does not forbid a body on any method (a `GET` may carry one, even if servers rarely act on it), so even the request-content channel is narrowed rather than shut. So the honest claim is *narrowing*, and every place the axis surfaces has to say so, or `GET`-only will be read as no-exfiltration by the first host that wants it to mean that. This is the single framing constraint the whole design bends around: literal HTTP verbs, no `read_only=True` sugar, and a docstring that states the limit where a caller reads it.

## The sharp constraint: method scoping is only honest at an API boundary

A network-layer proxy cannot see the method of an HTTPS request. A `CONNECT` tunnel hides everything past the handshake — which is exactly why the docker and wslc proxies' allowlists are host-level and can never be anything else. Method enforcement is honest **only where the request crosses an API boundary before TLS**: wasi-http in the Hyperlight guest, an SDK-level egress shim, or a MITM proxy (its own trust decision, not an implementation detail — the ACAS L7 proxy already inspects far enough to answer a denied host with `x-deny-reason: <host>:GET`).

So most backends will honestly never enforce this. The axis is not a gap to close in every backend — it is a refinement few backends will ever claim, and its central requirement is that **cannot** be declarable and refusable, not silently ignored. A backend that drops the methods and serves the host wide has degraded a `GET`-only spec into an anything-goes one, reported as a success — the exact failure [`egress-resolution.md`](egress-resolution.md) exists to refuse.

## Reframing the issue's forks against the shipped model

[#377](https://github.com/sokolaidev/maf-extensions/issues/377) was filed against the *old* egress model — a single `Egress` declaration on an asymmetry scale, where the router refused a backend that confined less than the spec asked. The model that shipped is different: a workload runs **one** `Egress` mode, a backend declares `egress_modes` (the **set** of postures it enforces), and the router does membership — `spec.egress in backend.egress_modes`, refuse otherwise. Method scope is **not a new posture**; it is a refinement of the `ALLOWLIST` *payload* (`egress_allow`). That reframing moves two of the issue's four forks.

### Fork 1 — spec-side shape: widen the element type

`egress_allow` becomes `tuple[str | EgressRule, ...]`. A bare string stays exactly what it is today — "this host, all methods" — and `EgressRule(host, methods)` narrows to named methods. Additive: every existing `("mcr.microsoft.com",)` keeps working unchanged, one field still carries the whole allowlist, and it is the issue's own lean. A parallel `Mapping` field would split one concept across two that must be kept in sync; forcing every entry to be an `EgressRule` is breaking churn for no gain.

### Fork 2 — backend-side declaration: a `Capability`, not a fourth `Egress` member

The issue proposed a fourth `Egress` member above `ALLOWLIST`, ordered the way `Isolation` is. Under the set model that reads badly: it would split "allowlist" into two postures and force `egress_modes` to carry a mode that is really an allowlist with an annotation. The refinement is a thing a backend **can do** to an allowlist — which is what `Capability` is for.

So: a new opt-in **`Capability.EGRESS_METHODS`**, not in `DEFAULT_CAPABILITIES`. A backend that enforces method scope at an API boundary declares it; one that cannot simply does not — the honest `cannot`, expressed as the absence of a capability rather than a claim it has to make and get wrong. This also avoids the fourth-bare-`getattr`-declaration trigger [`sandbox-architecture.md`](sandbox-architecture.md) warns about (a new `enforces_egress_methods` flag would be exactly that), because a `Capability` rides an axis the protocol already has.

### Fork 3 — router: no new logic

Because the declaration is a `Capability`, the refusal is the one the router already runs. A spec with any method-scoped entry **derives** `Capability.EGRESS_METHODS` into its `requires` at construction, and the existing capability match refuses a backend that lacks it with `SandboxCapabilityNotSupported` — no new `getattr`, no new exception, no new router branch. The egress **mode** still resolves independently (`ALLOWLIST` ∈ `egress_modes`); both axes must pass, so refuse-never-degrade holds on each and a method-scoped spec is never silently served host-wide. The derivation is the "cannot forget" guard: a kind that names methods cannot also fail to require the capability, because it does not write `requires` for this by hand.

### Fork 4 — normalization: mirror the hostname rules `egress_allow` already applies

`EgressRule` is validated at construction, each rule the least-surprising one and each a parallel of a check plain hostnames already get:

- **Casing** — methods are uppercased (`get` → `GET`), so `GET` and `get` cannot drift into two rules.
- **Token shape** — each method is a valid HTTP token, the RFC 9110 `tchar` grammar, which rejects whitespace and separators (`/`, `:`, `,`, and the rest) so a malformed entry like `GET/foo` or `GET:foo` is refused, while every real verb is a token and passes. There is still **no verb whitelist**: WebDAV and custom methods are real, and whether a *given* verb is enforceable is the backend's concern, not the vocabulary's. This validates the *grammar*, not membership in a list of known verbs — the parallel to the hostname check, which rejects delimiter-confusing forms without enumerating hosts.
- **Empty methods** — `EgressRule(host, methods=())` is a `ValueError`. `methods=None` means *all* (the bare-string equivalent); a non-empty tuple means *these*; an empty tuple would mean "allow this host for no method at all," which is incoherent — drop the host instead.
- **Unscoped rules collapse to strings** — `EgressRule(host, None)` is exactly a bare `host` (all methods), so `__post_init__` canonicalizes it *to* the string. A method annotation survives only when it actually narrows, which is what keeps the capability derivation exact: any `EgressRule` still present after construction carries real methods, so an all-methods entry never forces `Capability.EGRESS_METHODS` and never reaches a non-capable backend as anything but a plain host.
- **Scheme** — entries stay **schemeless**, as they are today. No `http://`/`https://` in the vocabulary, so nothing a spec spelled precisely can be silently widened across schemes; per-scheme behaviour is a backend's own (hyperlight registers both forms for a bare host, which is its normalization to make, not ours).
- **Duplicate host** — a host may appear **once**, compared **case-insensitively**. Hostnames are case-insensitive (the CONNECT proxy already matches its allowlist that way), so `Example.com` and `example.com` are one host and cannot carry two different method sets that a backend lowercasing the name would then silently combine. A bare string and an `EgressRule` for one host, or two `EgressRule`s for it, is a `ValueError`: which one wins is ambiguous, and it is the same "one entry per host" instinct the plain allowlist already carries.

## What each layer changes

- **`SandboxSpec`** — `egress_allow` widens to `tuple[str | EgressRule, ...]`. `__post_init__` keeps the existing "a non-empty `egress_allow` requires `egress is ALLOWLIST`" guard, adds the "one entry per host" refusal, and **auto-adds `Capability.EGRESS_METHODS` to `requires`** when any entry is method-scoped. This is the one place the model derives rather than validates, and it is safe because `requires` is an additive set.
- **`EgressRule`** — a new frozen dataclass in `_protocol.py`: `host: str`, `methods: tuple[str, ...] | None = None`. Validated as above. Its docstring is where the narrowing-not-closing honesty lives.
- **`Capability`** — a new `EGRESS_METHODS` member, opt-in, absent from `DEFAULT_CAPABILITIES`. Declared only by a backend that enforces method scope before TLS.
- **The router** — unchanged. The capability match it already runs is the whole refusal.
- **The backends** — a backend that serves a method-scoped spec (so, one declaring the capability) sees `str | EgressRule` in `egress_allow` and reads the methods; a backend that does not declare it is only ever handed plain hosts, because unscoped rules have collapsed to strings and any surviving `EgressRule` forces the capability. Where the methods have to land is **reuse identity**, and it differs by backend. **docker and wslc** both fold the allowlist into the sandbox name ([`../network.md`](../network.md) records both), so that fold must grow to include the methods, or a `GET`-only and an all-methods sandbox for one host collide into one warm container. **ACAS does not fold the allowlist at all** — its reuse key is `(scope, thread, agent_dir, kind)` and a warm sandbox keeps the policy of the spec that created it — so method scope inherits ACAS's existing changed-allowlist hazard and needs the same fix (a full-policy identity, or a replacement check on reuse), not a fold it does not have. None **declares** the capability today: the CONNECT-tunnel pair (docker, wslc) cannot see the method at all, ACAS's L7 proxy could in principle, and the first real declarer is Hyperlight.
- **Conformance** — a method-scope probe (a `GET` reaches a `GET`-only host, a `POST` to it is refused), extending [#402](https://github.com/sokolaidev/maf-extensions/issues/402)'s outcome probe, landing with the first backend that declares `EGRESS_METHODS`.

## Sequencing

Fully additive; nothing is blocked on it. The Hyperlight adapter serves host-wide entries under plain `ALLOWLIST` on day one and adopts the refinement when the axis lands — a `GET`-only spec is simply refused on it until then, which is the correct refuse-never-degrade behaviour, not a regression. The axis and the first declaring backend land together so the capability is never a claim nothing exercises.
