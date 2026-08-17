# 11 — two backends, one router: who serves, who cannot, who gets cleaned up

Every sample before this one hands `SandboxRouter` a single backend. This one registers two and shows the three things that follow.

```
app  ->  maf_sandbox (router)  ->  [ in-process | docker ]  ->  the sandbox
                                          ^ chosen at construction, not per call
```

## The short version

**Which backend serves is a deployment decision, made once.** `SandboxRouter(backends, selected="docker")` names it. Omit `selected=` and you get whichever was registered first. That is the whole mechanism — one argument, and the workload above never learns which one answered.

**A spec can raise the bar. It cannot change who serves.** If the selected backend cannot meet what a spec asks for, the router refuses. It does not look at the other backend, even when the other backend would obviously do. Act 2 shows both refusals with `docker` registered and unused.

**Disposal is the exception.** `dispose` and `dispose_scope` go to *every* registered backend, which is the one place holding more than one is live at run time.

## Why the refusals are the point

A reader who assumes the router reroutes has assumed a safety net that is not there — and the assumption fails in the reassuring direction, because what it imagines is something stronger quietly taking over.

The refusals in act 2 are not staged. The two backends declare genuinely different things, and every refusal follows from what they are rather than from configuration chosen to produce it:

| | `in-process` | `docker` |
|---|---|---|
| isolation | `none` | `container` |
| capabilities | `exec`, `files_in` | `exec`, `files_in`, **`files_out`** |

So a spec asking for `container` isolation gets `SandboxBackendNotPermitted`, and a spec requiring `FILES_OUT` gets `SandboxCapabilityNotSupported` — while a backend declaring both sits registered beside the one refusing. That pairing is also why this sample uses Docker rather than two in-process backends: with two instances of one class the difference would be something the sample invented, and the lesson would be worth less.

**Selecting per spec is [#328](https://github.com/sokolaidev/maf-extensions/issues/328)**, and `docs/design/two-axis-sandbox-policy.md` describes it as though it had shipped — that mismatch is [#329](https://github.com/sokolaidev/maf-extensions/issues/329). If #328 lands, this sample gains an act rather than needing a rewrite: the refusals become the *opted-out* behaviour and the reroute becomes visible beside them.

## Why disposal reaching both matters

It looks like housekeeping and it is not. A host that changes `selected=` between deployments — Docker on a laptop, ACAS in production — leaves sandboxes behind on whichever backend it was using before. If disposal followed the current selection only, those would stay running, and on a backend where a sandbox is billable that is a leak with a price on it.

Act 5 acquires one sandbox on each backend, with only `docker` serving, and shows `dispose_scope` returning **2**.

## The other axis, and why it refuses in one direction only

Isolation is the axis acts 1 and 2 are about. Egress is the other one the two-axis design names, and acts 3 and 4 are the only place in the sample set where it is shown.

The rule is not symmetrical, and the asymmetry is the idea. A backend that cannot confine as *precisely* as a spec asked still serves: `DockerSandboxConfig()` with no proxy declares `Egress.CLOSED`, so a spec allowing `mcr.microsoft.com` gets a container with no network at all. That is **more** confinement than was asked for, the router logs a warning naming the hosts that will be unreachable, and the workload fails loudly at the fetch. A backend that cannot confine **at all** — `Egress.UNRESTRICTED` — is refused instead, because silently widening what a workload reaches produces no symptom anybody sees. `Egress`'s own docstring is where that is written down; act 3 is where you can watch it.

Act 4 then enforces one for real. Set `egress_proxy_image` and the same backend class declares `Egress.ALLOWLIST`: each sandbox gets its own internal network and a dual-homed proxy, and the container has **no route out except that proxy**. The allowlisted host answers `200`; a host the spec never named answers `000`, which is curl reporting that no connection was made at all rather than a server saying no. Nothing inside the container was configured to cooperate — `HTTP_PROXY` tells ordinary clients where to look, and the topology is what enforces the list.

The proxy image is built rather than pulled, from a recipe that ships inside the package:

```bash
python -c "from maf_sandbox_docker import proxy_build_context; print(proxy_build_context())"
docker build -t maf-egress-proxy:local "$(python -c 'from maf_sandbox_docker import proxy_build_context; print(proxy_build_context())')"
export MAF_EGRESS_PROXY_IMAGE=maf-egress-proxy:local
```

Without that variable act 4 skips itself and prints those commands. The live check treats a skip as a failure, because a run that confined nothing prints every other line exactly as a run that confined something.

**One thing act 4 cannot show**, and it is worth knowing before adapting it: a kind that needs no network and a kind that was simply never asked both write `egress_allow=()` today, so this sample has to spell the empty case the same way whether it means "deny everything" or "no opinion". That conflation is [#403](https://github.com/sokolaidev/maf-extensions/issues/403).

## Run

```bash
cd samples/11_router_two_backends && uv run agent.py
```

Needs a Docker-compatible engine and nothing else — no cloud account, no model. One optional variable, `MAF_EGRESS_PROXY_IMAGE`, turns act 4 on; without it the other four acts run and act 4 says how to build the image. The container image is the same dev-container base samples 06 and 08 use, and the command run inside it is `cat` on a file the sample wrote: this is about which backend runs a command, not what the command is.

There is one thing in acts 4 and 5 worth knowing if you adapt it. A fresh container has nothing at `work_dir`, so `exec` cannot change into it — the sample writes a file first, which creates the parents. That ordering is not decoration; it is why every kind pushes its inputs before running anything.

## Where this sits

Sample 10 removed the sandbox and the model to show policy. This one puts a real container back and keeps the subject on policy: the router is the only thing in the stack whose job is to say no, and until now nothing showed it choosing between two things it could have said yes to.
