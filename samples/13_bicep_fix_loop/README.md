# 13 — two turns, one sandbox: validate, read the diagnostics, fix

Every other sample runs a single turn. The model is asked something, a tool answers, the program prints the reply and exits — so nothing has ever shown the thing `acquire` is get-or-create *for*: a second turn arriving to find its sandbox still there.

This one runs two turns and a check against the same key, and prints the container count after each.

| | What happens | Containers |
|---|---|---|
| Turn 1 | the model calls `bicep_validate` and reports what came back | 1 |
| Turn 2 | it edits `main.bicep` and validates again | 1 |
| The check | the sample compiles the file itself, with no model involved | 1 |

Three `acquire` calls, one container. A second container would have answered every one of those calls just as well, which is why the count is printed rather than described.

## The session is the mechanism

```python
session = agent.create_session()
first  = await agent.run("Validate main.bicep. …", session=session)
second = await agent.run("Fix the faults those diagnostics point at. …", session=session)
```

Turn 2's prompt says "those diagnostics" and names none of them. It does not have to: the session carries turn 1's tool result, so the model is repairing faults a compiler reported rather than faults the prompt described. Drop `session=` and turn 2 becomes a stranger to turn 1 — it would have to validate again before it could fix anything, and the sample would be two unrelated turns that happen to run in order.

## What is checked is the file, not the reply

A model will tell you it fixed something. The interesting question is whether the file moved.

So after the turns, the sample reads `main.bicep` back out of the file store and compares it with what went in. `main.bicep changed: True` cannot be produced by prose. Then it compiles that file — the same `bicep_validate` the model used, called directly from the program — and prints the compiler's own verdict beside the model's account of it.

That last step is also the third `acquire`, which is why it earns its own container count. Turn 2 finding the sandbox warm could be two calls landing close together; the check runs after all the model's work is done and still finds the same one.

## The approval gate that makes a fix turn do nothing

Worth knowing before you wire `FileAccessProvider` into anything headless, because the failure is silent.

`FileAccessProvider` has **two** approval switches, and they cover different tools:

```python
FileAccessProvider(
    store,
    disable_readonly_tool_approval=True,   # file_access_read and friends
    disable_write_tool_approval=True,      # file_access_write, _replace, …
)
```

Set only the write one — the obvious choice, since writing is the dangerous half — and the fix turn dies on the *read*. The model's first move is to read the file it is about to edit, that call raises an approval request, no human is present to answer it, and `agent.run` returns an empty string. No exception, no warning, no edit. Turn 1 still works perfectly, because validating never touches the file tools, so it reads as a model that lost interest rather than a program missing a flag.

## What the model is actually looking at

`main.bicep` is byte-identical to [sample 05's](../05_docker_bicep/) and reports the same three diagnostics through the same [image](../../images/bicep-sandbox/):

```
[error]   no-unused-params      @ main.bicep:21
[warning] BCP035                @ main.bicep:31
[warning] use-recent-api-versions @ main.bicep:31
```

The sample tracks **two** of them. `no-unused-params` and `BCP035` are structural — a parameter that is declared and never used, a resource missing a required property — so "fixed" means the same thing today and in a year.

`use-recent-api-versions` is not tracked, and that is deliberate. It fires on how old the API version is, so what counts as fixed moves with the calendar; requiring it to be fixed would rot, and requiring it to remain would forbid a genuine repair. The model sees it, may address it, and neither answer changes the result. Sample 01's README reads all three closely and that reading applies here unchanged — including why `no-unused-params` printing as `[error]` rather than its built-in `[warning]` is the visible proof `bicepconfig.json` was discovered.

Fixing either tracked fault is a real edit, and the sample reports which happened rather than demanding both.

## Counted, not claimed

Container counts come from `docker ps -a --filter label=maf-sandbox.thread=…` — the labels the backend stamps and `dispose_scope` selects on, with `-a` so a container stopped but not removed still counts.

The live check holds the file tally to the compiler **rule by rule**: a fault it calls fixed the compiler must no longer report, and one it calls remaining the compiler must still report. The tally is a substring search over the model's file, and a model that deletes the offending lines while breaking something else would satisfy it. That is the whole reason the compiler runs again at the end.

## Run

```bash
cd samples/13_bicep_fix_loop && uv run agent.py
```

Needs a Docker-compatible engine, the sandbox image, and a model.

```bash
docker build -t bicep-sandbox:local ../../images/bicep-sandbox
```

Build it fresh. A stale image is the one failure mode that looks like a healthy run: `bicepconfig.json` lives inside it, so an image from before the config was last touched simply reports fewer diagnostics, at their built-in severities, with nothing anywhere saying the rule set was old.

For the model, [sample 09's](../09_inprocess_bicep/) split unchanged — `AZURE_OPENAI_ENDPOINT` set means Azure OpenAI with a federated credential, unset means a local Ollama server at `http://localhost:11434/v1` with no configuration at all. `OPENAI_CHAT_MODEL` and `OPENAI_BASE_URL` override the local defaults.

The fix turn asks more of a model than any other sample here: it has to read diagnostics, decide what each one means for the source, and make the edit. A small local model can validate fine and still return nothing useful for turn 2. The output says exactly what the file looks like afterwards either way.

## Where this sits

Sample 05 runs this workload once. Sample 12 shows when a sandbox goes away. This one is the case they leave out — the sandbox that is still there because the conversation is not over, which is what get-or-create was for.
