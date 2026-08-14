# 10 — the host-tools contract: what a host declares, and what refuses it

The first sample with no model, no sandbox and no configuration. It runs in about a second on any machine with Python.

```
app  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
 ^ all of this happens here, before a sandbox exists
```

`Capability.HOST_TOOLS` is the one capability where trust crosses **outward**. Every other capability is about what a sandboxed program may reach; this one is about what it may reach *back into*. A dispatched body runs in the host process, with the host's authority, driven by model-written code, and its call is invisible to the host's middleware — the boundary sees only `execute_code`'s aggregate result.

So the interesting part is not the call. It is everything a host decides before one is possible, and what happens when a deployment decides no.

## Nothing is dispatched here, and that is not an omission

**No shipped backend declares `Capability.HOST_TOOLS`.** The constant lives in `maf_sandbox` — in the protocol, the registry and the router — and in none of `maf-sandbox-acas`, `-docker` or `-wslc`. The transport that would carry a request out of a guest and a response back is still being designed ([#133](https://github.com/sokolaidev/maf-extensions/issues/133)).

That is why [`agent.py`](agent.py) builds its own backend to show the permitted path. `InProcessSandboxBackend` takes its capabilities as a constructor argument, so the sample can hand it `HOST_TOOLS` and watch the router agree. Having to write that by hand is the honest demonstration: if a shipped backend declared it, the sample would not need to.

What is real today is the half a host configures on day one regardless — and it is the half that decides whether the other half ever runs.

## The four acts

Read [`host_tools.py`](host_tools.py) first. Four ordinary functions; three carry a declaration and one deliberately does not.

**1. Registration — the registry is the one door.** Nothing is dispatchable until it is registered, and `resolve` is the only path in, which is what makes registration the only place a gate can honestly sit. The registry here is built with `require_declared=True`, so the unstamped fourth function is refused *at registration* — the host's own configuration site, where the fix is one decorator away — rather than at dispatch, where the model would get a sanitized sentence and the host a tool it never classified.

The library default is `require_declared=False`, and the sample turns it on rather than relying on it. With the gate off, an unstamped tool registers and fails safe: read as an `UNTRUSTED` source and an `APP` identity, with `has_undeclared` raised in the aggregate so the degrade is visible.

The one-time registration notice is printed rather than suppressed. It says out loud that dispatched calls bypass middleware, and a reader meeting this channel for the first time is who it is for.

**2. What the surface means.** `registry.aggregate()` is not a summary of the three declarations — it is a different statement, about the one model-facing `execute_code` tool, derived per leg over the relevant subset:

| | On this registry | Why |
|---|---|---|
| `result_integrity` | `untrusted` | the weakest tier over **sources only** — `fetch_changelog` drags the result down and `semver_bump` cannot drag it back up. A registry with no sources has no opinion at all (`None`) and the workload's default stands |
| `outbound_caps` | `{'public'}` | every declared sink cap, verbatim and unfolded. Confidentiality is the host's own vocabulary and this library has no ordering for it, so two distinct caps are the host's to reconcile |
| `identities` | `{app, user}` | whose authority the bodies exercise |
| `requires_approval` | `True` | one `USER` tool is enough: a single dispatch may exercise the user's delegated authority |
| `has_undeclared` | `False` | the gate refused the fourth function |

**Taking the aggregate seals the registry.** A later `register` is refused, and the sample shows that refusal. It matters because this is the moment a host turns the surface into a spec and a classification — widening it afterwards would dispatch something no policy ever saw. It is also why `agent.py` carries `identities` forward from the aggregate instead of asking the registry twice: `HostToolAggregate.identities` is the only way to read it, precisely so that reading it has to seal.

**3. A host that permits it.** The aggregate's `identities` becomes `SandboxSpec.identities`, the spec requires `HOST_TOOLS`, and `router.ensure_can_serve(spec)` returns. That one call is the whole of a host's wiring test.

**4. The two refusals.** Two axes, two settings, both answered **at attach** — before any sandbox is acquired, so neither costs anything to demonstrate:

- `denied_capabilities={Capability.HOST_TOOLS}` → `SandboxCapabilityDenied`. This deployment does not want the outward channel at all, whatever backend is registered.
- `denied_identities={Identity.USER}` → `SandboxIdentityDenied`. The channel is fine; model-orchestrated *user* authority is not.

Both are `PermissionError`, both name the deployment's own setting, and both turn away the **whole kind**. There is no partial attach — a registry with one refused tool in it attaches nothing.

Act 4 ends by getting past the second refusal, and *how* is the part worth reading. Not a narrower call against the objects already built — three things prevent that, each on purpose:

| | |
|---|---|
| `SandboxSpec` is `@dataclass(frozen=True)` | its `identities` cannot be edited after the router has been handed it |
| `aggregate()` sealed the registry | a later `register` is refused, so the surface cannot widen under a policy derived from it |
| there is no unregister | `HostToolRegistry` exposes `register`, `resolve`, `declaration_for`, `names` and `aggregate`, and nothing that removes |

So narrowing means going back to the registration site and building the smaller surface from the start. The sample does exactly that: a second registry with `publish_release_note` left out folds to `identities={app}` and `requires_approval=False`, and the same `denied_identities` router serves a spec built from *that*.

Least privilege here comes from what a host **registers**, never from what it declares — and the three rows above are what that costs in practice.

## `Identity.USER` is declarable and not servable

`publish_release_note` declares `Identity.USER` and could never run. That is deliberate on both sides: declaring it must be possible so a registry can be written honestly and refused loudly, and serving it must not be until per-run token minting, an audience-within-egress check and an ephemeral exec env channel exist.

Declaring it as `APP` to make the refusal go away is exactly the lie the leg exists to prevent — and `APP` is not the safe option either. It is the application's full authority, every grant the deployed process holds.

## Run

```bash
cd samples/10_inprocess_host_tools && uv run agent.py
```

No environment variables, no `az login`, no container engine, no model. The [PEP 723](https://peps.python.org/pep-0723/) block names one dependency.

This is the only sample that reads no configuration — its `verify-live.yml` job is the only one with no `environment:` and no `permissions:` block for the same reason — so the `_scaffold.py` copy every sample carries goes unused here. It stays because [`tests/test_sample_scaffold.py`](../../tests/test_sample_scaffold.py) holds all ten copies byte-identical, and a sample that dropped it would be the one that drifts.

## Where this sits

Sample 09 removed the billable sandbox. This one removes the model as well, and what remains is the part of the integration you can put in CI on every pull request: a host's posture, its registry, and the router agreeing or refusing.

The dispatch counterpart — a program inside a real sandbox calling back out, with the round-trip cost measured — is [#302](https://github.com/sokolaidev/maf-extensions/issues/302), and it is blocked on [#133](https://github.com/sokolaidev/maf-extensions/issues/133).
