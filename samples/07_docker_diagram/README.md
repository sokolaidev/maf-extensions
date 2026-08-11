# 07 — render a diagram in a Docker container, and pull the image back out

The first sample that reads a file **back out** of the sandbox. Samples 05 and 06 write into a container and read its stdout; this one writes a Graphviz DOT source in, runs a renderer, and pulls the resulting PNG out through `FILES_OUT` — the pull surface the Docker backend added. The image never enters the transcript: the model gets a *reference* to where it landed, and the bytes go to host storage through a sink.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ this file's `render_diagram` calls the router,
                then `collect_outputs(...)` lands the PNG in `out/`.
```

## The workload is defined in the sample, not in a package — and that is the point

Samples 05 and 06 lean on a packaged kind: `maf_sandbox_bicep`'s `bicep_validate`, `maf_sandbox_codeact`'s `execute_code`. This one defines its kind — `render_diagram` — **inline in [`agent.py`](agent.py)**, and imports nothing from a workload package. Everything it needs is public in `maf_sandbox`: the `SandboxSpec` that says what sandbox to ask for, the `OutputSink` that lands bytes in host state, `sandboxed_tool` that wires the tool onto an agent, and `collect_outputs` that pulls the declared file back. So this file is what a third party writing their **own** sandbox kind against the published protocol would write — with nothing reached from inside the library. It is the layer beneath samples 05 and 06, shown once.

A kind is three things, and they are all in `agent.py`:

- **`diagram_sandbox_spec()`** — a `SandboxSpec` with `kind="diagram-generator"`, closed egress, and one `DeclaredOutput` (`diagram.png`, `image/png`, `required=False`). It `requires` `EXEC`, `FILES_IN` and — the new one — `FILES_OUT`, so the router refuses any backend without a pull surface before a container is ever created.
- **`render_diagram(dot)`** — the tool body. It writes the DOT in with `write_file`, runs `dot -Tpng` as a fixed argv (no shell, the model's source is a file argument), and on success calls `collect_outputs(...)` to land the PNG. A `dot` that rejects malformed DOT exits non-zero and produces no file; the body hands its diagnostic back for the model to fix, which is exactly why the output is `required=False`.
- **`make_png_sink(out_dir)`** — the `OutputSink`. Its `deliver` writes the bytes and returns a `LandedArtifact` whose `display` — a one-line "saved under `out/`" — is all the model sees, and whose `handle` — the real host path — is the host's own reference that nothing renders into the transcript. That split is a security property, not tidiness: a sink that returned one string, put in a tool result, could persist a path (or a signed URL) into the conversation to be replayed every turn.

## The image itself does not come back

`render_diagram` returns **where** the PNG landed, never the PNG. The model is told to report the location and not to claim it saw the picture, and the tool's result carries no image bytes to tempt it. This is the shape any "produce a file" workload wants: the artifact goes to storage the host controls, and the transcript carries a handle to it — not a base64 blob that bloats every subsequent turn and puts guest-produced bytes in the model's context.

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

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-docker agent-framework-openai azure-identity "azure-core[aio]"
```

There is **no workload package** to install — the kind is in `agent.py`, which is the point of the sample. `maf-sandbox` arrives as a dependency of the backend, which otherwise drives the `docker` client and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `DIAGRAM_SANDBOX_IMAGE` | The image built above — for example `diagram-sandbox:local`. An unqualified single-name tag: Docker resolves it to its official `docker.io/library/` namespace, which no third party can publish to, so if you skip the build the backend's pull fails cleanly rather than fetching a different image. Build it first and it runs from this machine. (To pin it to the local daemon regardless, qualify it — `localhost/diagram-sandbox:local` — and tag the build to match.) |
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |

With `DIAGRAM_SANDBOX_IMAGE` or either required model variable unset, the program says which and exits non-zero rather than running. That is deliberate: `make_diagram_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

```bash
python agent.py
```

The first call pays for creating and starting the container — a few seconds, against the minutes a microVM-isolated sandbox needs. `agent.py` prints only the model's reply and the disposal line — never `render_diagram`'s own result — so what you see looks something like this:

```
The diagram has been rendered and saved to `out/diagram.png`. It shows the
three-stage pipeline: ingest → transform → load.

Disposed 1 sandbox(es).
```

That block is one real run. **What the model says varies** — the DOT it writes, whether it labels the edges, how it phrases the reply. **What does not vary** is the tool result underneath it and the file on disk: `render_diagram` returns exactly

```
Rendered diagram.png (image/png); saved under out/.
```

every time — a host-authored line, not the model's — and a valid PNG appears at `out/diagram.png` (`89 50 4E 47` — the PNG magic — as its first bytes). `Disposed 1 sandbox(es).` prints only once `render_diagram` has actually created and torn down a container; a `Disposed 0` would mean the model answered without rendering anything, the T0 behaviour this sample exists to contrast with.

The PNG is git-ignored (`out/`), so a run leaves no tracked file behind.

## What has and has not been run against a live backend

**Run live**, on 2026-08-11: Docker Engine 29.5.3 with the `diagram-sandbox` image above (Graphviz 2.43.0), and a local tool-calling model behind an OpenAI-compatible endpoint — which is what this sample used at the time; it has since moved onto the same Azure OpenAI wiring as the rest of the set, and that run has not been repeated against it. The agent wrote DOT, `render_diagram` rendered it in a `--network none` container at `Isolation.CONTAINER`, and `collect_outputs` landed a valid 4–11 KB PNG at `out/diagram.png` — the full `FILES_IN → exec → FILES_OUT` round trip, end to end.

**Not run in CI — but no longer for a reason.** This sample was left out of `verify-live.yml` because it needed an endpoint and key that CI has no secret for. It now authenticates the way samples 01, 03, 05, 06 and 08 do, so that objection is gone and only the image build stands between it and a job: `images/diagram-sandbox` would have to be built on the runner first. Tracked on [#191](https://github.com/sokolaidev/maf-extensions/issues/191). The docker **backend** beneath it is exercised on every pull request by `test_docker_e2e.py` — including its `FILES_OUT` stat-and-read path — on the Linux runners that have Docker.

## Troubleshooting

**`Cannot connect to the Docker daemon`** — the client is installed but no daemon is reachable. Start Docker Desktop (or your engine) and confirm with `docker version`, which reports both a Client and a Server section when the daemon is up.

**`Error: ... image ... not found` / the render never happens** — `DIAGRAM_SANDBOX_IMAGE` names an image that is not on this machine. Build it (see prerequisites); the backend pulls an absent image before creating the container, and a single-name tag like `diagram-sandbox:local` resolves to Docker's official `library/` namespace, where this name is not published — so that pull fails rather than fetching something else, and the fix is to build the image locally.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `MICROVM` floor, and raises at construction rather than at first call.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what the spec requires: run a command, take a file in, and read one back. `DockerSandboxBackend` declares all three (`EXEC`, `FILES_IN`, `FILES_OUT`); this only appears against a swapped-in backend that declares less — which is the requirement doing its job, refusing before a container exists rather than failing inside one.

**`dot could not render the diagram (exit 1): ...`** — the model wrote DOT that Graphviz rejected. That is the diagnostic, handed back for the model to fix; it usually self-corrects on the next call. The declared output is `required=False`, so no PNG is produced and none is expected.

**A non-Unicode console (`UnicodeEncodeError` on Windows)** — the model's reply can contain characters like `→` that a legacy Windows code page (cp1252) cannot encode, and `print` then raises. Run under a UTF-8 stdout — WSL, or `set PYTHONIOENCODING=utf-8` — as CI and the other samples' platforms already do.
