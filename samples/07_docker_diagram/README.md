# 07 — render a diagram in a Docker container, and pull the image back out

The first sample that reads a file **back out** of the sandbox. Samples 05 and 06 write into a container and read its stdout; this one writes a Graphviz DOT source in, runs a renderer, and pulls the resulting PNG out through `FILES_OUT` — the pull surface the Docker backend added. The image never enters the transcript: the model gets a *reference* to where it landed, and the bytes go to host storage through a sink.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ this file's `render_diagram` calls the router,
                then `collect_outputs(...)` lands the PNG in `out/`.
```

## The workload is defined in the sample, not in a package — and that is the point

Samples 05 and 06 lean on a packaged kind: `maf_sandbox_bicep`'s `bicep_validate`, `maf_sandbox_codeact`'s `execute_code`. This one defines its kind — `render_diagram` — in **[`diagram_kind.py`](diagram_kind.py)** beside `agent.py`, and imports nothing from a workload package. Everything it needs is public in `maf_sandbox`: the `SandboxSpec` that says what sandbox to ask for, the `OutputSink` that lands bytes in host state, `sandboxed_tool` that wires the tool onto an agent, and `collect_outputs` that pulls the declared file back. So that file is what a third party writing their **own** sandbox kind against the published protocol would write — with nothing reached from inside the library. It is the layer beneath samples 05 and 06, shown once.

A kind is two things plus the seam it leaves for the host, and `diagram_kind.py` is exactly that — `agent.py` beside it is the same host wiring as every other sample, and imports one name from it:

- **`diagram_sandbox_spec()`** — a `SandboxSpec` with `kind="diagram-generator"`, closed egress, and `outputs_named_at_call_time=True`: this workload lands something, and cannot say here what its path will be, because the path carries the call's own run id. It `requires` `EXEC`, `FILES_IN` and — the new one — `FILES_OUT`, so the router refuses any backend without a pull surface before a container is ever created.
- **`render_diagram(dot)`** — the tool body. It takes the call's own directory from `session.guest_call_path()`, writes the DOT there with `write_file`, runs `dot -Tpng` as a fixed argv (no shell, the model's source is a file argument), and on success calls `collect_outputs(..., outputs=(DeclaredOutput(...),))` with the declaration it can only make now. A `dot` that rejects malformed DOT exits non-zero and produces no file; the body hands its diagnostic back for the model to fix, which is exactly why the output is `required=False`.
- **`make_diagram_tools(..., sink)`** — and the `sink` parameter is the interesting half. The kind does not build one. It takes an `OutputSink` from the host and passes it to `sandboxed_tool`, so where the bytes land is the application's decision and never the kind's; `agent.py` supplies `make_file_system_sink` from `maf_sandbox`. What the kind does own is the consequence of that split: `deliver` returns a `LandedArtifact` whose `display` — a one-line "saved under `out/`" — is all the model sees, while its `handle`, the real host path, stays the host's own reference and is never rendered into the transcript. That is a security property rather than tidiness: one string doing both jobs, put in a tool result, could persist a path or a signed URL into the conversation to be replayed every turn.

The factory passes `source_integrity=SourceIntegrity.UNTRUSTED`, so the tool says what its result is rather than leaving the answer to the input-label join and the host's `default_integrity`. The success reference is the host's own sentence, and that is not what settles it: [the rule](../../docs/sandbox/information-flow.md) turns on derivation, not on who authored the bytes. `dot`'s failure diagnostic quotes the model's own DOT source back, and which of the reference and the diagnostic comes back is decided by that source — a presence bit — so the result derives from input the framework has not established as trusted, and `trusted` is not available to declare. Declaring nothing hands the question to the framework's input-label join, which finds no labels on `dot: str` and answers untrusted, the same answer both packaged kinds land on.

## The image itself does not come back

`render_diagram` returns **where** the PNG landed, never the PNG. The model is told to report the location and not to claim it saw the picture, and the tool's result carries no image bytes to tempt it. This is the shape any "produce a file" workload wants: the artifact goes to storage the host controls, and the transcript carries a handle to it — not a base64 blob that bloats every subsequent turn and puts guest-produced bytes in the model's context.

## Every call gets a directory, and the framework takes it away

`acquire` is get-or-create, keyed by the caller's scope, thread and agent directory. Ordinary render bodies can overlap on one sandbox. This sample leaves confinement undeclared, so cleanup selects disposal, waits for active sibling calls to finish, and removes that sandbox before later calls can acquire it. Docker declares `RECLAIM`, but that alone cannot establish that Graphviz confines its filesystem and process effects to the call's directory.

Each render needs its own path under `work_dir`: that is where the framework tracks the call's inputs and outputs, and it avoids fixed-name collisions between overlapping renders. A real Graphviz filesystem and process probe is still owed before declaring confinement and enabling warm reuse after cleanup.

`SandboxToolSession.guest_call_path()` names a directory allocated for this call, under `work_dir`. `sandboxed_tool` awaits the selected cleanup after the body returns — after a result, a refusal and an exception alike. Here disposal removes the whole sandbox, including that directory. So:

- **Each render has distinct guest paths.** The kind needs no lock to keep its DOT and PNG filenames separate. The host explicitly wires `make_file_system_sink(existing="replace")`, so two renders in one message both land `out/diagram.png`; the last replacement wins, regardless of the order in which the renders started. A later local execution replaces it too. Replacement is this sample's choice; the sink's default refuses an occupied destination. A stable name is the deliberate half of `DeclaredOutput(name=…)`; the alternative puts the call's id into host storage and into the sentence the model reads.
- **Nothing to clean up in the kind.** The kind calls no removal of its own. `Sandbox.remove` requires `FILES_DELETE`; framework reclamation requires `RECLAIM` and a confined workload. The cleanup ladder selects a stronger established rung when reclamation is unavailable, and this sample uses disposal.
- **The landed name stays `diagram.png`.** The guest path carries a run id; host storage should not. `DeclaredOutput(path=f"{run_id}/diagram.png", name="diagram.png")` is what splits the two — `path` is where to read it in the guest, `name` is what it lands as, and without the second the run id ends up in `out/` and in the sentence the model is shown.

The price is that the spec no longer names the file at attach time. `outputs_named_at_call_time=True` says *this kind lands something* without saying what, which is weaker as documentation and exactly as strong as a check: `sandboxed_tool` still refuses to attach without an `OutputSink`, and still refuses a spec that lands anything without requiring `FILES_OUT`. Nothing moved out of the attach gate — only the filename did.

Reclamation is available only when the backend, workload and host's cleanup floor all permit it; directory allocation alone is not a confinement claim.

### File ownership and removal belong to the backend

A call directory that `write_file` created is the **container user's** on every image that identifies its user: `write_file` stamps the tar entries it sends with the image's configured user — resolved from the container's own account files over the pull surface — so the file, the call directory and every *missing* directory between it and `work_dir` land owned by the principal that runs in the guest and can be modified or reclaimed by it. A parent that already exists keeps the ownership and mode it had, and an absent ancestor *above* `work_dir` is docker's to create as root; neither is the call directory, which is what has to be reclaimable. On an image whose only unusual line is `USER app`, that used to mean every call left its directory behind — `rm: cannot remove '…/note': Permission denied`, for the life of the conversation, with the framework disposing the sandbox after each failed reclaim so the next call started cold as well.

**[#684](https://github.com/sokolaidev/maf-extensions/pull/684) and [#680](https://github.com/sokolaidev/maf-extensions/issues/680) settled that in the backend**, which is where a fix belongs. A kind could have worked around it — one `mkdir` from the guest before the first write, and the directory belongs to the user that will remove it — and this sample deliberately never did, because that puts an OS command and a POSIX assumption inside a kind; [#585](https://github.com/sokolaidev/maf-extensions/issues/585) is the standing argument for taking those *out* of the layers above the backend, not adding more. `remove` and `reclaim` run as `--user 0`, but only over a path with no component the guest could have swapped — the protocol's reach rule, set out in [`docker.md`](../../docs/sandbox/backends/docker.md) — and the writes themselves are the guest's, so the reach rule costs nothing on the images this sample runs. This kind knows nothing about any of it.

An image that hands the guest a directory *above* `work_dir` drops Docker's removals back to the guest's authority. An image whose configured user cannot be resolved is refused for this sample's `FILES_OUT` requirement; the warned root-owned upload fallback is available only to input-only workloads. ACAS and WSLC cannot establish safe ancestry for host-authority reclamation and explicitly refuse it with `NotImplementedError` ([ACAS #1088](https://github.com/sokolaidev/maf-extensions/pull/1088), [WSLC #1036](https://github.com/sokolaidev/maf-extensions/pull/1036)). They withhold `RECLAIM` and `SNAPSHOT`, so router-managed cleanup disposes their sandboxes.

**And it wires the failure path, which is the half with a decision in it.** `agent.py` builds its router with a `ReclaimConfig`: a `timeout`, a `failed_reclaim_policy` and an `on_failure` handler. All three are decisions, and a host leaving them at their defaults has still made them.

The policy is the one with a blast radius, and the two halves fail in opposite directions. `DISPOSE` — the default, stated explicitly here rather than inherited — has the framework *try* to end the sandbox when a call's directory could not be removed, so the conversation's next call starts cold; a disposal that does not land is reported as `failed`, and the router then refuses that key with `SandboxUnclean` rather than handing on what it could not clean. That refusal is **this process's** knowledge and no further: the docstring says so — *"another replica holds no such record"* — and this backend derives a stable container name it will reuse, so a fresh run of this sample, or another replica, can reacquire the same dirty sandbox. The refusal buys the rest of the conversation, not the data; what actually removes it is a disposal that lands, which is why the router keeps trying.

`KEEP` does not try. It leaves the sandbox warm with the unremovable data still in it, and that is where the directory nobody could remove stays readable by every later call in the conversation — `acquire` is get-or-create, so it is handed back rather than rebuilt, and disposal is the only thing that would have removed it. The conversation continues; the data is what pays for it.

The handler is **not** where that is decided, and the order is what a reader gets wrong first: the framework acts, *then* calls it, and `ReclaimFailure.disposal` says which of `disposed`, `failed` or `kept` already happened. So it is where a host logs, counts and pages — nothing more. It writes to stderr here, because `ReclaimFailure.path` is a guest path and host-side detail rather than something the model should see; and a handler that raises is caught and logged rather than replacing the call's own answer, since it runs in a `finally`.

Where safety actually lives is neither: a disposal that does not land leaves the router refusing that key with `SandboxUnclean` until one does.

The `timeout` is the decision whose cost is easy to miss. It is not one bound on cleanup but the same number applied three times over — the removal gets it, the disposal that follows a failed removal gets it again, and the handler above gets it a third time — so a single unclean call can hold the turn for three times what the field appears to say. 30 seconds is the default, written out here for that reason. And the wait is not free: `sandboxed_tool` awaits all three in the `finally` of the tool call itself, so the body has produced its answer but the call has not returned it yet. The model is still waiting, and so is whoever is waiting on the model. A host tuning this is tuning tool-call latency, in units of three.

The run prints `Reclaim failures this turn: N` from that handler's own count, and the live check requires nought. That is the framework's answer about the directories this turn made, rather than a probe of the guest: a kind spelling OS commands to prove a framework guarantee teaches the wrong thing. What it does not show is the handler *firing* — a healthy run reports nought every time, and forcing one needs a removal that can be made to fail on demand, which only the in-process backend offers.

## The boundary is weaker, and the refusal is the feature

**`DockerSandboxBackend` declares `Isolation.CONTAINER`**, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — so `agent.py` opts the floor down explicitly to `Isolation.CONTAINER`, and the default would refuse this backend outright. A Docker Desktop or Colima VM does not lift that rung: one shared VM kernel serves every container. That is a reasonable place to run a renderer on a disposable graph, and the wrong place to put next to a deployment's credentials; the router draws the line for you and will not be argued out of it without saying so in code, at construction time.

**Egress is closed, and that costs this workload nothing.** `diagram_sandbox_spec()`'s `egress_allow` is empty — `dot` reads the source it was given and writes an image, and reaches nothing — so the docker backend's closed-by-default network (`--network none`) asks for exactly what this workload already wanted. There is no allowlist to fall short of.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Docker Desktop (macOS, Linux, Windows with WSL 2) or Docker Engine (Linux). `docker version` confirms the client can reach a running daemon.
- **The `graphviz-sandbox` image, built locally.** It uses a digest-pinned Wolfi base with Graphviz — see [`images/graphviz-sandbox`](../../images/graphviz-sandbox/). Build it once, from the repository root so the build context is that directory:

  ```bash
  docker build -t graphviz-sandbox:local images/graphviz-sandbox
  ```

- **An Azure OpenAI deployment.** No key: the sample authenticates with `DefaultAzureCredential`, so an `az login` session — or a federated credential in CI — is enough. The model has to write DOT and call one tool; that is the whole demand on it. Samples 02 and 04 are the ones that keep the key-and-base-URL client, because a local server (Ollama, vLLM, LM Studio) is the case that needs it.

No preview enrolment and no billable sandbox — the container is free. A run killed mid-turn leaves the container **running** (it was started with `sleep infinity`, and nothing stops it on the way out), so `docker ps` shows it and `docker rm -f <name>` reclaims it.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

There is **no workload package** to install — the kind is `diagram_kind.py`, right here, which is the point of the sample. `maf-sandbox` arrives as a dependency of the backend, which otherwise drives the `docker` client and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `DIAGRAM_SANDBOX_IMAGE` | The image built above — for example `graphviz-sandbox:local`. An unqualified single-name tag: Docker resolves it to its official `docker.io/library/` namespace, which no third party can publish to, so if you skip the build the backend's pull fails cleanly rather than fetching a different image. Build it first and it runs from this machine. (To pin it to the local daemon regardless, qualify it — `localhost/graphviz-sandbox:local` — and tag the build to match.) |
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |
| `SAMPLE_BACKEND` | Optional. `docker` (the default) or `docker-sbx`; see [On Docker Sandboxes](#on-docker-sandboxes) |

With `DIAGRAM_SANDBOX_IMAGE` or either required model variable unset, the program says which and exits non-zero rather than running. That is deliberate: `make_diagram_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The first call pays for creating and starting the container — a few seconds, against the minutes a microVM-isolated sandbox needs. `agent.py` prints the model's reply and its own three tagged lines — what it resolved, how many call directories it could not clean, and what it disposed — and never `render_diagram`'s own result, so what you see looks something like this:

```
  [measured] installed: maf-sandbox 0.24.0, maf-sandbox-docker 0.8.1
The image was saved under `out/diagram.png`.

  [measured] Disposed 1 sandbox(es).
```

That block is one real run, on 2026-08-26, and it is reproduced as it was recorded — which is why the reclaim line is absent from it: that run predates the handler this sample now wires. A run today prints `[measured] Reclaim failures this turn: 0` between the reply and the disposal line. Nothing else about it has changed, and the line was not pasted in after the fact, because a transcript this sample invites you to compare your own output against is worth no more than its accuracy.

**What the model says varies** — the DOT it writes, whether it labels the edges, how it phrases the reply, and whether it repeats the tool's own sentence or writes its own as it did here. **What does not vary** is the tool result underneath it and the file on disk: `render_diagram` returns exactly

```
Rendered diagram.png (image/png); saved under out/.
```

every time — a host-authored line, not the model's — and a valid PNG appears at `out/diagram.png` (`89 50 4E 47` — the PNG magic — as its first bytes).

**The landed PNG proves that the renderer ran.** `Disposed N` reports only what the final scope purge removed, so `Disposed 0` is expected when per-call cleanup already disposed the sandbox. The checker still requires the tagged purge report and the reclaim-failure count; a missing image, a reclaim failure or a `Not fully disposed:` report fails independently of the purge count.

A `Not fully disposed:` report means the sweep could not account for everything. It does not prove a container survived: the backend can also report a failed label query when there is nothing to find. The checker reports that failure as data that may remain.

Each of those lines carries `[measured]` because it is the sample's report rather than the model's, and the reply is filtered before printing so a line of it starting with that tag comes out quoted, `> [measured] …` — otherwise a reply writing "Disposed 1 sandbox(es)." would answer for the router ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)).

The PNG is git-ignored (`out/`), so a run leaves no tracked file behind.

## What has and has not been run against a live backend

**Run live**, on 2026-08-11: Docker Engine 29.5.3 with the image then named `diagram-sandbox` (Graphviz 2.43.0), and a local tool-calling model behind an OpenAI-compatible endpoint — which is what this sample used at the time. The agent wrote DOT, `render_diagram` rendered it in a `--network none` container at `Isolation.CONTAINER`, and `collect_outputs` landed a valid 4–11 KB PNG at `out/diagram.png` — the full `FILES_IN → exec → FILES_OUT` round trip, end to end.

**Run live again**, on 2026-08-26, on the Azure OpenAI wiring the rest of the set uses and on the call-directory shape above: Docker Engine 29.7.2, `gpt-5.4-mini`, `maf-sandbox 0.24.0` and `maf-sandbox-docker 0.8.1`. A 353×59 PNG landed and `scripts/check_live_diagram_sample.py` passed on that transcript. **That run predates the block a reader resolves today**: the floor has since moved to `maf-sandbox>=0.25`, not for anything this sample uses — `guest_call_path()`, `outputs_named_at_call_time` and `DeclaredOutput.name` were all in 0.24.0 — but because the sample set moved together. Nothing here has been re-run against 0.25.

**Measured separately, and not from this sample**: the ownership behaviour above, on three images (root, non-root, non-root with the work directory pre-owned) against three kind shapes. [#680](https://github.com/sokolaidev/maf-extensions/issues/680) carries that table; nothing in `samples/` reproduces it, because doing so would mean shipping an image built to be wrong.

**Gated in CI.** `verify-live.yml` builds the image above on the runner and runs this sample on demand and once after each release of `maf-sandbox` or `maf-sandbox-docker`. Its check reads the landed PNG's own header rather than the model's account of it (`scripts/check_live_diagram_sample.py`): a turn that describes a diagram it never rendered writes the same paragraph as one that did, so the file is the evidence. The docker **backend** beneath it is exercised more often still — `test_docker_e2e.py` runs a real container on every pull request, `FILES_OUT` stat-and-read path included.

## On Docker Sandboxes

`SAMPLE_BACKEND=docker-sbx` renders in a Docker Sandboxes microVM on this machine, through [`maf-sandbox-docker-sbx`](../../packages/maf-sandbox-docker-sbx/README.md). That backend is `MICROVM`, so the router keeps its default floor. It needs `sbx` installed and signed in, with SSH agent forwarding off and no MCP server registered; the backend's README says how. The `graphviz-sandbox` image already has what the backend needs, so it loads as it is built:

```bash
docker build -t graphviz-sandbox:local images/graphviz-sandbox
docker save -o graphviz-sandbox.tar graphviz-sandbox:local
sbx template load graphviz-sandbox.tar
SAMPLE_BACKEND=docker-sbx DIAGRAM_SANDBOX_IMAGE=graphviz-sandbox:local uv run samples/07_docker_diagram/agent.py
```

`verify-live.yml` runs this variant as its own job, against the same check, after each release of `maf-sandbox` or `maf-sandbox-docker-sbx`.

## Troubleshooting

**`Cannot connect to the Docker daemon`** — the client is installed but no daemon is reachable. Start Docker Desktop (or your engine) and confirm with `docker version`, which reports both a Client and a Server section when the daemon is up.

**`Error: ... image ... not found` / the render never happens** — `DIAGRAM_SANDBOX_IMAGE` names an image that is not on this machine. Build it (see prerequisites); the backend pulls an absent image before creating the container, and a single-name tag like `graphviz-sandbox:local` resolves to Docker's official `library/` namespace, where this name is not published — so that pull fails rather than fetching something else, and the fix is to build the image locally.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `MICROVM` floor, and raises at construction rather than at first call.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what the spec requires: run a command, take a file in, and read one back. `DockerSandboxBackend` declares all three (`EXEC`, `FILES_IN`, `FILES_OUT`); this only appears against a swapped-in backend that declares less — which is the requirement doing its job, refusing before a container exists rather than failing inside one.

**`dot could not render the diagram (exit 1): ...`** — the model wrote DOT that Graphviz rejected. That is the diagnostic, handed back for the model to fix; it usually self-corrects on the next call. The declared output is `required=False`, so no PNG is produced and none is expected.

**A non-Unicode console (`UnicodeEncodeError` on Windows)** — the model's reply can contain characters like `→` that a legacy Windows code page (cp1252) cannot encode, and `print` then raises. Run under a UTF-8 stdout — WSL, or `set PYTHONIOENCODING=utf-8` — as CI and the other samples' platforms already do.

## Qualify the published image through the SDK

**Selected release: Graphviz 0.1.1; hosted SDK and hardened CLI qualification passed.** [Run 38041599180](https://github.com/sokolaidev/maf-extensions/actions/runs/38041599180) verified the release identity and passed both sets of checks described below. Historical 0.1.0 results remain evidence only for that earlier digest.

The manual [Graphviz consumer qualification workflow](https://github.com/sokolaidev/maf-extensions/actions/workflows/graphviz-sdk-qualification.yml) invokes this sample's decorated `render_diagram` tool directly, without a model or Azure credentials. It installs published `maf-sandbox==0.48.0`, `maf-sandbox-docker==0.27.0` and `agent-framework-core==1.20.0` in a clean environment. Core 0.48.0 satisfies that Docker release's `<0.49` bound; the workspace's core version is not substituted. The workflow runs only when manually dispatched on `main`, retains reports on success or failure for 90 days, and adds no image execution to ordinary PR CI. It does not build or publish an image.

[qualify.py](qualify.py) first verifies the selected release's provenance, SBOM and completion using the existing consumer verifier. [graphviz-policy.json](graphviz-policy.json) pins the independently selected Graphviz 0.1.1 identity from the [consumer guide](../../images/graphviz-sandbox/README.md); review that selection before running. Update the policy and published SDK pins deliberately when qualifying a different combination. The publication attempt number is inspection context, not an authenticated identity field. Current monitoring status is reported separately and does not override successful release identity verification.

The qualification passes only when the real router and Docker backend deliver a PNG through `FILES_OUT`, reject invalid DOT without delivering a PNG, and remove their containers after both calls. It checks PNG chunk checksums, exact decompressed scanline sizes and filter bytes for noninterlaced 8-bit RGB/RGBA output; other layouts and critical chunks are refused. It observes the digest reference and `network=none` on each acquired container, and checks that the timeout probe has no active interface other than loopback (inactive kernel tunnel devices are permitted). A separate SDK `sleep 30` execution must time out with a one-second limit, return within 20 seconds including disposal, and leave no container. This is a deterministic backend timeout check; it does not test the renderer's timeout-message branch. A final scope purge runs on failure, but cannot turn a failed SDK cleanup check into a pass.

With `--hardened-cli`, the harness also exercises the [consumer guide's Docker CLI controls](../../images/graphviz-sandbox/README.md#render-a-png-with-docker): the non-root host UID/GID, closed network, read-only root and input, dropped capabilities, disabled privilege escalation, bounded CPU/memory/processes and tmpfs scratch. It inspects the container before starting `dot`, using `docker create` and `start --attach` with the guide's payload and settings. The renderer must exit successfully within 30 seconds, leave no container before fallback cleanup, and deliver a validated PNG. The deadline is enforced by the host; cleanup has its own bounded commands. A render failure, timeout, invalid PNG or leftover container remains a failure even if fallback removal succeeds. The hosted workflow always includes this check.

To reproduce both checks on a Linux host with a local Linux/amd64 Docker engine and a non-root host user, use a reviewed checkout, `uv`, and an authenticated GitHub CLI. The output directory must not already exist. These Bash commands retain the PNG and JSON evidence in a fresh temporary directory:

```bash
QUALIFICATION_ROOT="$(mktemp -d)"
gh release download image-graphviz-v0.1.1 --repo sokolaidev/maf-extensions \
  --dir "$QUALIFICATION_ROOT/evidence"
uv run --isolated --no-project --python 3.12 samples/07_docker_diagram/qualify.py \
  --policy samples/07_docker_diagram/graphviz-policy.json \
  --hardened-cli --evidence "$QUALIFICATION_ROOT/evidence" --output "$QUALIFICATION_ROOT/result"
```

`qualification.json` records the selected image identity, verification and monitoring results, installed package versions, Docker/Python versions, checkout commit and dirty state, harness/sample hashes, container observations and each check's outcome. Source or editable SDK installations are refused. The optional `hardenedCli` section records observed CLI controls, timing, PNG details and cleanup; the CLI PNG is retained at `cli-output/diagram.png`. On Windows, omit `--hardened-cli` to run the SDK checks with Docker Desktop in Linux-container mode.

This exercises the sample tool and SDK lifecycle for one image and package combination. It does not call an agent/model, qualify Docker Sandboxes microVMs, test every diagram, or establish production-wide security. The default Docker SDK runs this image as root with a writable root filesystem and drops all capabilities; this qualification records those settings. The separate [hardened Docker CLI example](../../images/graphviz-sandbox/README.md) uses different settings. Retain the report with your own application qualification before adopting the digest.

**Measured locally on 2026-10-09 (Europe/Amsterdam):** Python 3.12.13, the published package versions above, and Docker Desktop 29.8.2 serving Linux/amd64 containers passed all four checks against Graphviz 0.1.0 manifest `sha256:c96936644f7dc2e9fa04a21ffc3274fbf52e5c045e978a5e27a2edf80a531e63`. The sink delivered a 174×251 PNG (13,000 bytes, SHA-256 `379481be23dc0e677a5ec5f5353e0d679ef1a2f50f51a5a9c13fa1f88e34011d`); invalid DOT delivered none. Every observed container used `network=none`; the network probe's only active interface was loopback. The one-second timeout returned after 1.27 seconds including disposal, and no owned containers remained after any check or final cleanup. This is local SDK evidence for that image and package combination.

**Measured on GitHub-hosted Linux on 2026-10-09 (Europe/Amsterdam):** [Qualification run 37909959442](https://github.com/sokolaidev/maf-extensions/actions/runs/37909959442) passed from clean source `5d7cddf300933e253fd73d343f5a3b3705858f35`, using Python 3.12.3, Docker 28.0.4 on Linux/amd64 and the same published SDK versions and Graphviz 0.1.0 digest above. Release identity verification, PNG delivery, invalid-DOT rejection, closed networking and container disposal passed. The delivered PNG had the same dimensions, size and SHA-256 as the local result. The one-second timeout returned after 1.04 seconds including disposal, and final cleanup left no owned containers. The run retains `qualification.json` and `render/diagram.png` in its `graphviz-sdk-qualification` artifact for 90 days. Monitoring was reported clean at verification time; consult the current status report for later assessments. This qualifies the recorded combination only; a replacement image digest requires its own SDK check.

**Measured on GitHub-hosted Linux on 2026-10-10 (Europe/Amsterdam):** [Qualification run 38001025079](https://github.com/sokolaidev/maf-extensions/actions/runs/38001025079) passed from clean source `f3c0f4b1ce8a80b680351c14efe2273c625b2eae`, using Python 3.12.3, Docker 28.0.4 on Linux/amd64 and the same published SDK versions above against Graphviz 0.1.1 manifest `sha256:b44a268e61780d3c9020dbe6cb0cf791903553e4e23061dc0a36fafe6c2f113e`. Release identity verification, PNG delivery, invalid-DOT rejection, closed networking and disposal passed. The PNG was 174×251, 13,000 bytes, SHA-256 `379481be23dc0e677a5ec5f5353e0d679ef1a2f50f51a5a9c13fa1f88e34011d`. The one-second SDK timeout returned after 1.06 seconds including disposal, with no owned containers after any check or final cleanup. The run retains `qualification.json` and `render/diagram.png` in its `graphviz-sdk-qualification` artifact for 90 days. Monitoring was clean at verification time. This run predates the hardened CLI extension and supplies no CLI evidence.

**SDK and hardened CLI measured on GitHub-hosted Linux on 2026-10-10 (Europe/Amsterdam):** [Qualification run 38041599180](https://github.com/sokolaidev/maf-extensions/actions/runs/38041599180) passed from clean source `dea67d12b128450c3b72159cad22af5ffe107a83`, using Python 3.12.3, Docker 28.0.4 on Linux/amd64, the published SDK versions above and the selected Graphviz 0.1.1 digest. Release identity and all four SDK checks passed again; the SDK PNG matched the earlier 0.1.1 result, and the one-second SDK timeout returned after 1.05 seconds including disposal. The CLI ran as UID/GID `1001:1001` with `network=none` and no other attached network, a read-only root and input, all capabilities dropped with none added, privileged mode disabled, `no-new-privileges`, two CPUs, 1 GiB memory, 256 processes and the documented 64 MiB tmpfs. Rendering completed in 0.16 seconds within the 30-second deadline and produced a 171×251 PNG, 12,685 bytes, SHA-256 `eb95cd23c1b01ae2cfcc7d5f42dca0176ffddaa09aa48a95b4c000bafa41aa85`. No CLI container remained before fallback cleanup, no fallback removal was needed, and final SDK and CLI cleanup left no owned containers. Monitoring was clean at verification time. The [retained artifact](https://github.com/sokolaidev/maf-extensions/actions/runs/38041599180/artifacts/11665827429) contains `qualification.json`, `render/diagram.png` and `cli-output/diagram.png` for 90 days; both downloaded PNG hashes were independently checked against the report. This confirms successful CLI rendering under the recorded controls, not a live CLI timeout-failure scenario or a production deployment.
