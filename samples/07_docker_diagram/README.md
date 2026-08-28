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

## The image itself does not come back

`render_diagram` returns **where** the PNG landed, never the PNG. The model is told to report the location and not to claim it saw the picture, and the tool's result carries no image bytes to tempt it. This is the shape any "produce a file" workload wants: the artifact goes to storage the host controls, and the transcript carries a handle to it — not a base64 blob that bloats every subsequent turn and puts guest-produced bytes in the model's context.

## Every call gets a directory, and the framework takes it away

The sandbox outlives the call. `acquire` is get-or-create, keyed by `(scope, thread_id, agent_dir)`, so the container that served this render is the same one that serves the next — that is the point of it, and it is what makes *where a kind writes* a decision rather than a detail.

Write at a fixed path under `work_dir` and both halves of that reuse work against you. Two `render_diagram` calls in one assistant message run **concurrently** — the framework does not serialise them — so both would write one `diagram.dot` and read one `diagram.png`, and one call could collect the other's image. And every call would leave its source and its render sitting there for every later call in the conversation to read: the model's own input, and the picture made from it, retained for the life of the thread by nothing more than an oversight.

`SandboxToolSession.guest_call_path()` is the answer to both at once. It names a directory allocated for this call, under `work_dir`, and `sandboxed_tool` removes it and everything under it when the body returns — after a result, a refusal and an exception alike. So:

- **No lock.** Two renders share no *guest* path, so neither can read the other's source or collect its image, and the kind needs nothing to make that true. An earlier version of this sample held an `asyncio.Lock` around `write → exec → collect`; the lock was the price of the fixed guest path, and it is gone with it. What the two still share is the landed name: `make_file_system_sink` writes `root / name`, so two renders in one message both land `out/diagram.png` and the second overwrites the first. That is the sink's behaviour rather than this kind's, and the lock never changed it — the old code serialised the sequence and still landed one file. A stable name is the deliberate half of `DeclaredOutput(name=…)`; the alternative puts the call's id into host storage and into the sentence the model reads.
- **Nothing to clean up.** The kind calls no removal of its own. `Sandbox.remove` is a capability (`FILES_DELETE`) that only some backends serve; the reclaim of a call's own directory is not — every backend does it, and a kind that writes here gets it for free.
- **The landed name stays `diagram.png`.** The guest path carries a run id; host storage should not. `DeclaredOutput(path=f"{run_id}/diagram.png", name="diagram.png")` is what splits the two — `path` is where to read it in the guest, `name` is what it lands as, and without the second the run id ends up in `out/` and in the sentence the model is shown.

The price is that the spec no longer names the file at attach time. `outputs_named_at_call_time=True` says *this kind lands something* without saying what, which is weaker as documentation and exactly as strong as a check: `sandboxed_tool` still refuses to attach without an `OutputSink`, and still refuses a spec that lands anything without requiring `FILES_OUT`. Nothing moved out of the attach gate — only the filename did.

### What makes this reclaimable is the backend, not the kind

A call directory that `write_file` created is the **container user's** on every image that identifies its user: `write_file` stamps the tar entries it sends with the image's configured user — resolved from the container's own account files over the pull surface — so the file, the call directory and every *missing* directory between it and `work_dir` land owned by the principal that runs in the guest and can be modified or reclaimed by it. A parent that already exists keeps the ownership and mode it had, and an absent ancestor *above* `work_dir` is docker's to create as root; neither is the call directory, which is what has to be reclaimable. On an image whose only unusual line is `USER app`, that used to mean every call left its directory behind — `rm: cannot remove '…/note': Permission denied`, for the life of the conversation, with the framework disposing the sandbox after each failed reclaim so the next call started cold as well.

**[#684](https://github.com/sokolaidev/maf-extensions/pull/684) and [#680](https://github.com/sokolaidev/maf-extensions/issues/680) settled that in the backend**, which is where a fix belongs. A kind could have worked around it — one `mkdir` from the guest before the first write, and the directory belongs to the user that will remove it — and this sample deliberately never did, because that puts an OS command and a POSIX assumption inside a kind; [#585](https://github.com/sokolaidev/maf-extensions/issues/585) is the standing argument for taking those *out* of the layers above the backend, not adding more. `remove` and `reclaim` run as `--user 0`, but only over a path with no component the guest could have swapped — the protocol's reach rule, set out in [`docker.md`](../../docs/sandbox/backends/docker.md) — and the writes themselves are the guest's, so the reach rule costs nothing on the images this sample runs. This kind knows nothing about any of it.

**One limitation remains, and it is why this kind writes through the file plane.** An image that hands the guest a directory *above* `work_dir` drops its removals back to the guest's authority; the writes are the guest's now, so that costs nothing on the images this suite runs. And an image that names its user but answers no identity probe — no readable `/etc/passwd`, no `id` — leaves the writes at root's, where the old behavior lives; the backend logs that fallback at acquire. `wslc` and `acas` have the same two-principal split, and both settled it the way this backend did — `wslc` raises authority for its removals ([#706](https://github.com/sokolaidev/maf-extensions/pull/706)), `acas` moves `reclaim` to its data plane, which already acts as the host ([#707](https://github.com/sokolaidev/maf-extensions/pull/707)).

**And it wires no `on_reclaim_failure`.** `sandboxed_tool` takes a host callback for a removal that did not happen, and this sample passes none. The callback is a notification and not a control point: the body has returned by the time it runs, so the answer has already gone to the model, and a handler that raises is logged and contained. With the reclaim above now landing on this backend, there is nothing left here for one to report. What a deployment does with the fact where it still happens — count it, page when `ReclaimFailure.disposal` is `"failed"` and the router is refusing the conversation until a disposal lands — is operations code, and `packages/maf-sandbox/README.md` is where that surface is described.

## The boundary is weaker, and the refusal is the feature

**`DockerSandboxBackend` declares `Isolation.CONTAINER`**, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — so `agent.py` opts the floor down explicitly to `Isolation.CONTAINER`, and the default would refuse this backend outright. A Docker Desktop or Colima VM does not lift that rung: one shared VM kernel serves every container. That is a reasonable place to run a renderer on a disposable graph, and the wrong place to put next to a deployment's credentials; the router draws the line for you and will not be argued out of it without saying so in code, at construction time.

**Egress is closed, and that costs this workload nothing.** `diagram_sandbox_spec()`'s `egress_allow` is empty — `dot` reads the source it was given and writes an image, and reaches nothing — so the docker backend's closed-by-default network (`--network none`) asks for exactly what this workload already wanted. There is no allowlist to fall short of.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Docker Desktop (macOS, Linux, Windows with WSL 2) or Docker Engine (Linux). `docker version` confirms the client can reach a running daemon.
- **The `diagram-sandbox` image, built locally.** It is a Debian base with Graphviz and nothing else — see [`images/diagram-sandbox`](../../images/diagram-sandbox/). Build it once, from the repository root so the build context is that directory:

  ```bash
  docker build -t diagram-sandbox:local images/diagram-sandbox
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
| `DIAGRAM_SANDBOX_IMAGE` | The image built above — for example `diagram-sandbox:local`. An unqualified single-name tag: Docker resolves it to its official `docker.io/library/` namespace, which no third party can publish to, so if you skip the build the backend's pull fails cleanly rather than fetching a different image. Build it first and it runs from this machine. (To pin it to the local daemon regardless, qualify it — `localhost/diagram-sandbox:local` — and tag the build to match.) |
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |

With `DIAGRAM_SANDBOX_IMAGE` or either required model variable unset, the program says which and exits non-zero rather than running. That is deliberate: `make_diagram_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The first call pays for creating and starting the container — a few seconds, against the minutes a microVM-isolated sandbox needs. `agent.py` prints the model's reply and its own two tagged lines — what it resolved, and what it disposed — and never `render_diagram`'s own result, so what you see looks something like this:

```
  [measured] installed: maf-sandbox 0.24.0, maf-sandbox-docker 0.8.1
The image was saved under `out/diagram.png`.

  [measured] Disposed 1 sandbox(es).
```

That block is one real run, on 2026-08-26. **What the model says varies** — the DOT it writes, whether it labels the edges, how it phrases the reply, and whether it repeats the tool's own sentence or writes its own as it did here. **What does not vary** is the tool result underneath it and the file on disk: `render_diagram` returns exactly

```
Rendered diagram.png (image/png); saved under out/.
```

every time — a host-authored line, not the model's — and a valid PNG appears at `out/diagram.png` (`89 50 4E 47` — the PNG magic — as its first bytes). The disposal line prints only once `render_diagram` has actually created and torn down a container; a `Disposed 0` would mean the model answered without rendering anything, the T0 behaviour this sample exists to contrast with.

It carries `[measured]` because it is the sample's report rather than the model's, and the reply is filtered before printing so a line of it starting with that tag comes out quoted, `> [measured] …` — otherwise a reply writing "Disposed 1 sandbox(es)." would answer for the router ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)).

The PNG is git-ignored (`out/`), so a run leaves no tracked file behind.

## What has and has not been run against a live backend

**Run live**, on 2026-08-11: Docker Engine 29.5.3 with the `diagram-sandbox` image above (Graphviz 2.43.0), and a local tool-calling model behind an OpenAI-compatible endpoint — which is what this sample used at the time. The agent wrote DOT, `render_diagram` rendered it in a `--network none` container at `Isolation.CONTAINER`, and `collect_outputs` landed a valid 4–11 KB PNG at `out/diagram.png` — the full `FILES_IN → exec → FILES_OUT` round trip, end to end.

**Run live again**, on 2026-08-26, on the Azure OpenAI wiring the rest of the set uses and on the call-directory shape above: Docker Engine 29.7.2, `gpt-5.4-mini`, `maf-sandbox 0.24.0` and `maf-sandbox-docker 0.8.1`. A 353×59 PNG landed and `scripts/check_live_diagram_sample.py` passed on that transcript. **That run predates the block a reader resolves today**: the floor has since moved to `maf-sandbox>=0.25`, not for anything this sample uses — `guest_call_path()`, `outputs_named_at_call_time` and `DeclaredOutput.name` were all in 0.24.0 — but because the sample set moved together. Nothing here has been re-run against 0.25.

**Measured separately, and not from this sample**: the ownership behaviour above, on three images (root, non-root, non-root with the work directory pre-owned) against three kind shapes. [#680](https://github.com/sokolaidev/maf-extensions/issues/680) carries that table; nothing in `samples/` reproduces it, because doing so would mean shipping an image built to be wrong.

**Gated in CI.** `verify-live.yml` builds the image above on the runner and runs this sample on demand and once after each release of `maf-sandbox` or `maf-sandbox-docker`. Its check reads the landed PNG's own header rather than the model's account of it (`scripts/check_live_diagram_sample.py`): a turn that describes a diagram it never rendered writes the same paragraph as one that did, so the file is the evidence. The docker **backend** beneath it is exercised more often still — `test_docker_e2e.py` runs a real container on every pull request, `FILES_OUT` stat-and-read path included.

## Troubleshooting

**`Cannot connect to the Docker daemon`** — the client is installed but no daemon is reachable. Start Docker Desktop (or your engine) and confirm with `docker version`, which reports both a Client and a Server section when the daemon is up.

**`Error: ... image ... not found` / the render never happens** — `DIAGRAM_SANDBOX_IMAGE` names an image that is not on this machine. Build it (see prerequisites); the backend pulls an absent image before creating the container, and a single-name tag like `diagram-sandbox:local` resolves to Docker's official `library/` namespace, where this name is not published — so that pull fails rather than fetching something else, and the fix is to build the image locally.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `MICROVM` floor, and raises at construction rather than at first call.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what the spec requires: run a command, take a file in, and read one back. `DockerSandboxBackend` declares all three (`EXEC`, `FILES_IN`, `FILES_OUT`); this only appears against a swapped-in backend that declares less — which is the requirement doing its job, refusing before a container exists rather than failing inside one.

**`dot could not render the diagram (exit 1): ...`** — the model wrote DOT that Graphviz rejected. That is the diagnostic, handed back for the model to fix; it usually self-corrects on the next call. The declared output is `required=False`, so no PNG is produced and none is expected.

**A non-Unicode console (`UnicodeEncodeError` on Windows)** — the model's reply can contain characters like `→` that a legacy Windows code page (cp1252) cannot encode, and `print` then raises. Run under a UTF-8 stdout — WSL, or `set PYTHONIOENCODING=utf-8` — as CI and the other samples' platforms already do.
