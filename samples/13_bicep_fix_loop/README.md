# 13 — two turns, one sandbox: validate, read the diagnostics, fix

Every other sample runs a single turn. The model is asked something, a tool answers, the program prints the reply and exits — so nothing has ever shown the thing `acquire` is get-or-create *for*: a second turn arriving to find its sandbox still there.

This one runs two turns and a check against the same key, and prints the container count after each.

| | What happens | `bicep_validate` calls | Containers |
|---|---|---|---|
| Turn 1 | the model calls `bicep_validate` and reports what came back | ≥1 | 1 |
| Turn 2 | it edits `main.bicep` and validates again | ≥1 | 1 |
| The check | the sample compiles the file itself, with no model involved | 1 | 1 |

At least three `acquire` calls, one container. Nothing stops a model from validating twice in a turn — that is a normal thing for one to do, and it makes no difference to the claim, since the second call finds the same sandbox as the first. So the sample prints what happened and the check requires at least one call per turn rather than exactly one. Only the final compile is fixed at one, because the program makes that call itself.

A second container would have answered every one of those calls just as well, which is why the count is printed rather than described.

**The call counts are there because the container count cannot carry the claim alone.** A fix turn that edits the file and never validates it makes no second `acquire` at all — and turn 1's container is still sitting there to be counted, so the run would read as reuse while never demonstrating any. The counts come from the tool calls in each turn's returned messages, so "the fix turn reached the same warm sandbox" is a measurement rather than an inference.

## The session is the mechanism

```python
session = agent.create_session()
first  = await agent.run("Validate main.bicep. …", session=session)
second = await agent.run("Fix the faults those diagnostics point at. …", session=session)
```

Turn 2's prompt says "those diagnostics" and names none of them. It does not have to: the session carries turn 1's tool result, so the model is repairing faults a compiler reported rather than faults the prompt described. Drop `session=` and turn 2 becomes a stranger to turn 1 — it would have to validate again before it could fix anything, and the sample would be two unrelated turns that happen to run in order.

## What is checked is the file, not the reply

A model will tell you it fixed something. The interesting question is whether the file moved.

So after the turns, the sample compiles the file the model left — the same `bicep_validate`, called directly from the program — and everything it reports afterwards is read off that. Which faults remain is the compiler's answer, not a search of the source text.

That distinction decides whether a real repair passes. `no-unused-params` is satisfied two ways: delete `environmentName`, or start using it. A substring test for `param environmentName` calls the second one unfixed while the compiler calls the file clean — a genuine fix failed by its own harness. Asking the compiler costs nothing here, because it has already been run.

Two things the compiler cannot answer, so the sample keeps them separate.

`main.bicep changed: True` compares the file store against what went in. A model that edits nothing still compiles.

`storage account and output intact: True` is the one that closes the degenerate case. Replace `main.bicep` with an empty but valid file and every other signal agrees it was repaired: the file changed, no tracked fault is reported, and both compile phases come back clean. "Repaired" would be the verdict on a file with the storage account deleted. So the sample checks that the resource and the output it exists to produce are still there — a question about what the file is *for*, which a compiler has no opinion on.

All four of these lines are read out of the closing block rather than the whole run. The model is answering into the same stream and can write "faults fixed" in its own prose; that prose is printed above the block, so a search over everything would find the narration first and grade the run on it — this sample's own thesis, reintroduced in its checker.

The compile is also the third `acquire`, which is why it earns its own container count. Turn 2 finding the sandbox warm could be two calls landing close together; the check runs after all the model's work is done and still finds the same one.

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

**Everything else the compiler reports is a failure.** Tracking two rules and checking only those would pass a file whose original faults are gone and which now fails on something new — a fresh `BCP0xx` names neither tracked rule, so nothing would object, and the run would report a clean repair over a broken file. So the live check sweeps every diagnostic: a rule is acceptable only if the tally already calls it remaining, or it is the age rule above.

## Counted, not claimed

Container counts come from `docker ps -a --filter label=maf-sandbox.thread=…` — the labels the backend stamps and `dispose_scope` selects on, with `-a` so a container stopped but not removed still counts.

Every other number is the compiler's or the file store's. The live check reads them back and requires them to agree with each other: a fault the tally calls fixed the compiler must no longer report, and one it calls remaining the compiler must still report. Two halves of the same output describing different files is the failure that catches.

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
