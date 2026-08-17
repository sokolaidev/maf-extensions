# 11 — two backends, one router: who serves, who cannot, what they can reach, who gets cleaned up

Every sample before this one hands `SandboxRouter` a single backend. This one registers two and shows the three things that follow — and then, in act 4, the other axis the router decides on.

```
app  ->  maf_sandbox (router)  ->  [ in-process | docker ]  ->  the sandbox
                                          ^ chosen at construction, not per call
```

## The short version

**Which backend serves is a deployment decision, made once.** `SandboxRouter(backends, selected=DOCKER_BACKEND)` names it. Omit `selected=` and you get whichever was registered first. That is the whole mechanism — one argument, and the workload above never learns which one answered.

`selected=` is matched against a backend's `name`, and each backend package exports its own so a host reading that choice out of configuration does not have to build a backend to learn it ([#411](https://github.com/sokolaidev/maf-extensions/issues/411)):

```python
from maf_sandbox_docker import BACKEND_NAME as DOCKER_BACKEND
```

Aliased at the import because every backend package exports that same symbol — a host registering Docker beside ACAS would otherwise have the second `from … import BACKEND_NAME` shadow the first, with nothing to say so. The in-process backend has no constant, deliberately: its name is a constructor parameter with a default, so it belongs to whoever registers it, and the sample reads it back with `.name`.

**A spec can raise the bar. It cannot change who serves.** If the selected backend cannot meet what a spec asks for, the router refuses. It does not look at the other backend, even when the other backend would obviously do. Act 2 shows both refusals with `docker` registered and unused.

**Disposal is the exception.** `dispose` and `dispose_scope` go to *every* registered backend, which is the one place holding more than one is live at run time.

**And egress is decided by the deployment, not the workload.** Act 4 runs one agent against one Bicep file twice, changing nothing but whether the backend has an egress proxy — and the compiler succeeds once and fails once.

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

## Act 4: the workload that makes egress a real question

Act 3 ends on a promise — *the workload will report what it could not fetch*. Act 4 is where that stops being a sentence.

An agent validates `main.bicep`, which contains one line that changes everything:

```bicep
module storage 'br/public:avm/res/storage/storage-account:0.9.1' = {
```

`br/public:` means the compiler cannot type-check that module until it has **downloaded** it. So this file cannot be validated in a closed sandbox — not because a rule says so, but because the work is impossible. Sample 05's `main.bicep` is the deliberate opposite: no modules, nothing to restore, validates entirely offline. The pair is the point.

**The allowlist is the workload's, and the sample does not write it.** `bicep_sandbox_spec()` names four hosts, and the sample prints them by reading them back:

| host | why |
|---|---|
| `mcr.microsoft.com` | the registry the module artifact comes from |
| `*.data.mcr.microsoft.com` | where that registry redirects the blob download |
| `aka.ms` | the module index's published address, which redirects |
| `live-data.bicep.azure.com` | where it redirects to |

Both redirect hops have to be allowed; the redirector alone answers with a `Location` pointing at a host that is still denied. [#7](https://github.com/sokolaidev/maf-extensions/issues/7) is where each of the four was established.

**Act 4 runs the same agent twice, and changes exactly one thing.** Same file, same spec, same instructions, same model — only the deployment's wiring differs:

| | `DockerSandboxConfig()` | `DockerSandboxConfig(egress_proxy_image=…)` |
|---|---|---|
| declares | `Egress.CLOSED` | `Egress.ALLOWLIST` |
| container gets | `--network none` | an internal network and a dual-homed proxy |
| the compiler says | `BCP192: Unable to restore the artifact … (Resource temporarily unavailable (mcr.microsoft.com:443))` | no diagnostics |

Nothing inside the container was configured to cooperate. `HTTP_PROXY` only tells ordinary clients where to look; the container has **no route out except the proxy**, so the topology enforces the list rather than the variables.

**Both halves carry the claim, and neither is redundant.** A container with the host's network would restore under both postures. A container that could not start at all would fail under both. Only the pair says the wiring is what decided it — which is why the live check requires `FAILED` closed *and* `RESTORED` allowlisted, and treats a skipped act 4 as a failure. A run that confined nothing prints every other line exactly as a run that confined something.

What act 4 does **not** try to show is that the proxy denies an unlisted host. That is the backend's own property, pinned by `TestAllowlistEgress` in `maf-sandbox-docker`, and repeating it here with `curl` would have been a unit test wearing a sample's clothes. What only a sample can show is the consequence: real work that succeeds or fails on the deployment's egress posture.

**Two things worth knowing before adapting it.** A kind that needs no network and a kind that was simply never asked both write `egress_allow=()` today, so the empty case is spelled the same way whether it means "deny everything" or "no opinion" — that conflation is [#403](https://github.com/sokolaidev/maf-extensions/issues/403). And pass `egress_proxy_image=None` rather than `""` for the closed posture: an empty string declares `CLOSED` and then fails trying to start a proxy, which is [#407](https://github.com/sokolaidev/maf-extensions/issues/407).

## Run

Acts 1, 2, 3 and 5 need a Docker-compatible engine and nothing else. Act 4 needs two locally built images and a chat model, and skips itself with instructions when any of them is missing.

```bash
# The Bicep sandbox, built from this repository — the same guest sample 05 uses.
docker build -t bicep-sandbox:local images/bicep-sandbox

# The egress proxy, built from a recipe that ships inside the installed package
# rather than pulled as an image you would have to trust.
docker build -t maf-egress-proxy:local \
  "$(python -c 'from maf_sandbox_docker import proxy_build_context; print(proxy_build_context())')"

export BICEP_SANDBOX_IMAGE=bicep-sandbox:local
export MAF_EGRESS_PROXY_IMAGE=maf-egress-proxy:local
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_CHAT_MODEL=<your-deployment-name>

az login   # the model is reached with DefaultAzureCredential; there is no API key here

cd samples/11_router_two_backends && uv run agent.py
```

The image acts 1, 2 and 5 use is the same dev-container base samples 06 and 08 use, and the command run inside it is `cat` on a file the sample wrote: those acts are about which backend runs a command, not what the command is.

There is one thing in acts 4 and 5 worth knowing if you adapt it. A fresh container has nothing at `work_dir`, so `exec` cannot change into it — the sample writes a file first, which creates the parents. That ordering is not decoration; it is why every kind pushes its inputs before running anything.

## Where this sits

Sample 10 removed the sandbox and the model to show policy. This one puts a real container back and keeps the subject on policy: the router is the only thing in the stack whose job is to say no, and until now nothing showed it choosing between two things it could have said yes to.
