# 16 — CodeAct with the guest's output withheld, and read back out of a store (Docker)

The first sample where the model **cannot read what its program printed** and gets the answer anyway. Sample 08 wires both file channels and lets `stdout` come back; this one turns `stdout` off and replaces it with a second store the model can read and cannot write.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ maf_sandbox_codeact calls the router
```

## What is new against sample 08

Three arguments and one extra pair of tools:

- **`withhold_guest_output=True`** keeps everything the program wrote to `stdout` and `stderr` out of the result. What comes back is one line saying whether it exited cleanly — no exit code, no size for either stream — and one standing sentence. No text the program printed reaches the transcript; file content still can, through the read-back tools below.
- **`output_sink=make_file_store_sink(outputs, provenance=…)`** lands each call's declared outputs at `<call_id>/<name>` in an `AgentFileStore` rather than in a host directory. The folder is the host-minted call id, so one call's `summary.md` can never answer for the next call's — and a destination that already exists is refused rather than overwritten.
- **`sandbox_outputs_read_tools(outputs)`** gives the model `sandbox_outputs_ls` and `sandbox_outputs_read` over that store, and nothing else.

The withheld result then names the folder instead of listing which declared names landed, and the model goes and reads it.

## What to watch

**The grand total in the reply, beside a read that returned the landed file, is proof of the whole path.** Nothing the program printed comes back, so the total did not come from `stdout`. What says it came out of the file is the pair: the check requires a read whose result *was* the bytes the sink landed under this call's folder, and the total in the answer. The total alone would not say it — whether the program exited cleanly is a bit the *program* chooses, and repeated calls make that a channel, which is why withholding is a narrower road rather than no road ([`../../docs/sandbox/kinds/codeact.md`](../../docs/sandbox/kinds/codeact.md)). It is still a stronger claim than sample 08's, where a right answer only proves the program ran.

**The result names a folder, not a list of names.** Sample 08's withheld cousin would say `Saved: summary.md`. This one says where the folder is and stops. That is not tidiness: whether each declared name landed is a bit the *program* chooses, and a model that wants to smuggle a value out of a withheld result encodes into exactly those bits. The folder is a host-minted `uuid4` that takes no input from the arguments or from what the program did, so it carries none.

**The `[measured]` block is the host's, and the fenced read-backs are the tool's.** The model answers into the same stream as the sample, so the sample takes the tag away from anything the model said before printing its own lines. The read-backs are fenced because a model can *claim* it read the file; it cannot put the file's own text inside a block the tool's results closed.

## Two stores, and why that is the interesting decision

Sample 08 points its sink at a plain host directory and says: not the file store. This sample says something narrower and more useful — **not the *working* store**.

The working store holds what the program is given. The outputs store holds what it produced. They are two objects, and the reason is the one sample 08 gives: these bytes were authored by model-written code, so a sink pointed at the store the agent's own file tools write to has handed that code an unapproved write, and one that can overwrite has given it a way to influence a *different* tool on the next call. The per-call folder answers the overwrite half; the second store answers the rest.

Nothing model-facing is wired over the working store here at all, so `execute_code`'s own `files` parameter — bounded by the caller's listing — is the only road into the sandbox.

## Why the read-back tools are not a second `FileAccessProvider`

The obvious wiring is a second `FileAccessProvider(store=outputs, disable_write_tools=True)`. It does not work, and the failure is silent.

`FileAccessProvider` names its seven tools from fixed class constants, referenced as `FileAccessProvider.READ_TOOL_NAME` and friends *inside* the tool decorators rather than off the instance — so a subclass overriding them changes nothing. Two providers over two stores put ten tools into one run, with `file_access_read`, `file_access_ls` and `file_access_grep` appearing twice. The framework's own uniqueness check rejects that list; the path context providers take does not run it, so nothing raises and the model is handed a name it cannot use to say which store it means.

`sandbox_outputs_read_tools` is two tools with names of their own, `name_prefix` for a host that has two output stores, and **read-only by construction rather than by a flag** — there is no write, delete or replace here to disable.

## What withholding does and does not promise once this is wired

Worth reading twice, because the composition changes the claim.

`withhold_guest_output=True` on its own keeps guest-authored text out of the result. Wiring the read-back tools beside it **does not keep that text away from the model** — it moves it off a channel the guest encodes bits through and onto a path the host classifies. The two read-back tools carry no label of their own, so their results resolve through the host's `default_integrity` and its information-flow middleware, exactly as the framework's file tools do, behind whatever `approval_mode` the host chose.

That is the trade the composition is for, and it is a different promise from the one the mode makes alone. [`docs/sandbox/kinds/codeact.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/codeact.md) § *Withholding guest output* and [`docs/sandbox/hosts.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md) § *A sink the model can read back from* carry it.

## Prerequisites

- A Docker-compatible engine (Docker Desktop, colima, podman with the Docker socket).
- An Azure OpenAI deployment. No key: authentication is `DefaultAzureCredential`, so an `az login` session or a federated CI credential is enough.

Nothing is built. The image is `mcr.microsoft.com/devcontainers/python:3.13-bookworm`, pulled the way any `docker run` pulls it.

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |

## Run it

```bash
uv run --no-project samples/16_docker_codeact_outputs_store/agent.py
```

The last lines are the host's own: what was disposed, and what landed in the outputs store this turn — as JSON, because an artifact name may legally contain a comma.
