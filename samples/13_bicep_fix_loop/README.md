# 13 — author, validate, fix: two turns against one sandbox

Every other sample runs a single turn against a file that was already there. The model is asked something, a tool answers, the program prints the reply and exits — so nothing has ever shown the thing `acquire` is get-or-create *for*: a second turn arriving to find its sandbox still there.

Here the file store starts **empty**. There is no `main.bicep` in this directory; the model writes one.

| | What happens | validations reaching the sandbox | Containers |
|---|---|---|---|
| Turn 1 | the model writes `main.bicep` from a brief, then validates what it wrote | ≥1 | 1 |
| The baseline | the program compiles what turn 1 left, with no model involved | 1 | 1 |
| Turn 2 | it repairs what the compiler reported, and validates again | ≥1 | 1 |
| The check | the program compiles the result, again with no model involved | 1 | 1 |

At least four `acquire` calls, one container. Nothing stops a model from validating twice in a turn — that is a normal thing for one to do, and it makes no difference to the claim, since the second call finds the same sandbox as the first. So the sample prints what happened and the check requires at least one call per turn rather than exactly one. The two compiles are fixed at one apiece, because the program makes those calls itself.

A second container would have answered every one of those calls just as well, which is why the count is printed rather than described — and the container's **id** is printed beside it, because a count says one sandbox existed at that instant and the claim is that the same one served all four. A backend that force-removes a sandbox on an exec timeout leaves the next `acquire` to create a replacement, and every count still reads 1. The live check requires the four ids to agree.

**The validation counts are there because the container count cannot carry the claim alone.** A fix turn that edits the file and never validates it makes no second `acquire` at all — and turn 1's container is still sitting there to be counted, so the run would read as reuse while never demonstrating any. The counts come from each turn's returned messages, and count only calls whose result carries both compiler phases: `bicep_validate` refuses a bad filename before it acquires anything, so counting requests would score a rejected call as a reacquisition.

## The session is the mechanism

```python
session = agent.create_session()
first  = await agent.run(f"Write main.bicep with {SPEC}. Then validate it …", session=session)
second = await agent.run("Fix the faults those diagnostics point at. …", session=session)
```

Turn 2's prompt says "those diagnostics" and names none of them. It does not have to: the session carries turn 1's tool result, so the model is repairing faults a compiler reported rather than faults the prompt described. Drop `session=` and turn 2 becomes a stranger to turn 1 — it would have to validate again before it could fix anything, and the sample would be two unrelated turns that happen to run in order.

It does name the *goal*: leave the file reporting nothing it did not report before. That is not scripting the repair — it says nothing about how — and without it a model can finish honestly while having traded one diagnostic for another. The first live run did exactly that, fixing `no-unused-params` by moving `environmentName` into a variable it then left unused, reporting the new `no-unused-vars` and stopping.

## What is checked is the file, not the reply

A model will tell you it fixed something. The interesting question is whether the file moved.

So after the turns, the sample compiles the file the model left — the same `bicep_validate`, called directly from the program — and everything it reports afterwards is read off that. Which faults remain is the compiler's answer, not a search of the source text.

That distinction decides whether a real repair passes. `no-unused-params` is satisfied two ways: delete `environmentName`, or start using it. A substring test for `param environmentName` calls the second one unfixed while the compiler calls the file clean — a genuine fix failed by its own harness. Asking the compiler costs nothing here, because it has already been run.

Two things the compiler cannot answer, so the sample keeps them separate.

`main.bicep authored in turn 1: True` and `main.bicep changed by turn 2: True` are both read off the store — the first says a file appeared where there was none, the second says the fix turn moved it. A model that edits nothing still compiles.

`storage account and output intact: True` is the one that closes the degenerate case. Replace `main.bicep` with an empty but valid file and every other signal agrees it was repaired: the file changed, no tracked fault is reported, and both compile phases come back clean. "Repaired" would be the verdict on a file with the storage account deleted. So the sample checks that the resource, the output it exists to produce, and the two parameters that supply its name and location are all still there — a question about what the file is *for*, which a compiler has no opinion on. `environmentName` is deliberately not among them: deleting *that* is a valid repair of `no-unused-params`, while hardcoding what the other two supply repairs nothing.

Every number the sample measures is printed with a `[measured]` tag, and the live check takes its numbers only from tagged lines. This is the one sample where a model writes into the same stream the check parses, so a reply mentioning "containers after turn 2: 2" is otherwise indistinguishable from the count — and it is the model's reply that comes first. The tag also tells a reader of the log which lines are the harness speaking.

Both program-side compiles are `acquire` calls too, which is why each earns its own container count. Turn 2 finding the sandbox warm could be two calls landing close together; the last of the four runs after all the model's work is done and still finds the same one.

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

The brief in `agent.py` asks for the template [sample 05](../05_docker_bicep/) checks in — three parameters, one storage account, one output — so a compliant model produces a file that reports the same three diagnostics through the same [image](../../images/bicep-sandbox/):

```
[error]   no-unused-params      @ main.bicep
[warning] BCP035                @ main.bicep
[warning] use-recent-api-versions @ main.bicep
```

**Nothing in the brief calls them faults.** It asks for an `environmentName` parameter "which a later change will use", and for no `sku` "because the tier is still being decided" — both ordinary things to write in a real template, and between them they produce the first two. Naming the faults instead would script the repair, which is the thing [#304](https://github.com/sokolaidev/maf-extensions/issues/304) rules out: the point is a model reacting to real diagnostics.

Because the file is the model's, how many tracked faults it arrives with is measured rather than assumed. The sample prints `tracked faults in the authored file`, and the live check requires it to be at least one — an authored file that came out clean leaves the fix turn nothing to do, and would otherwise pass every assertion about reuse while demonstrating no fix loop at all.

**That baseline is the program's own compile of the snapshot, not a quote of turn 1's validation**, and the difference is not cosmetic. A model is free to validate a draft, edit it, and validate again — the sample permits more than one call per turn. Its *first* result then describes a file that no longer exists, and measuring against it would credit turn 2 with faults turn 1 had already fixed: two repairs attributed to a turn that changed a comment. Compiling the snapshot makes the diagnostics correspond to the file by construction.

The sample tracks **two** of them. `no-unused-params` and `BCP035` are structural — a parameter that is declared and never used, a resource missing a required property — so "fixed" means the same thing today and in a year.

`use-recent-api-versions` is not tracked, and that is deliberate. It fires on how old the API version is, so what counts as fixed moves with the calendar; requiring it to be fixed would rot, and requiring it to remain would forbid a genuine repair. The model sees it, may address it, and neither answer changes the result. Sample 01's README reads all three closely and that reading applies here unchanged — including why `no-unused-params` printing as `[error]` rather than its built-in `[warning]` is the visible proof `bicepconfig.json` was discovered.

Fixing either tracked fault is a real edit, and the sample reports which happened rather than demanding both. In practice a model often repairs `no-unused-params` by *using* the parameter rather than deleting it — a tag on the resource, say — which is exactly why the tally asks the compiler instead of searching the source.

**A fault is a rule and what it is about.** `BCP035` on a missing `sku` and `BCP035` on a missing `location` share a rule id and are not the same fault. Counting by rule id made a turn that traded one for the other subtract to nothing, and the run then reported that it had fixed none — while its own two diagnostic blocks showed that one had gone. That is [#432](https://github.com/sokolaidev/maf-extensions/issues/432), and it happened on a release. The tally now reads the target out of the message and reports `BCP035(sku)`, so the two compiles are compared element by element and `faults fixed`, `faults remaining` and `faults introduced` divide between them what the two said. A message the sample does not recognise leaves the fault as its bare rule id, which is what it counted before.

**A trade still fails.** Turn 2 is asked to leave the file reporting nothing it did not report before, so anything introduced is a loop that did not converge. What changed is the sentence: the failure names what went and what arrived, rather than claiming nothing was removed.

**Everything else the compiler reports is a failure.** Tracking two rules and checking only those would pass a file whose original faults are gone and which now fails on something new — a fresh `BCP0xx` names neither tracked rule, so nothing would object, and the run would report a clean repair over a broken file. So the live check sweeps every diagnostic. A tracked fault is acceptable if the tally calls it remaining or introduced — introduced being a failure in its own right, accounted for rather than tolerated. Any other rule is acceptable if the **authored file already reported it as a warning, no more often than it did** — turn 2 is asked to leave the file reporting nothing it did not report before, and an ordinary authoring tic like `simplify-interpolation` is not this turn's doing. Warnings only, because a file that does not *build* was never repaired whoever introduced the error; and counted, because one baseline warning licenses one, not any number.

**Nor does silencing count.** `#disable-next-line no-unused-params` above the parameter and the same for `BCP035` above the resource makes both diagnostics vanish with no `sku` added and the parameter still unused — verified against the pinned CLI, which then reports only the age warning. Every other signal reads that as a repair: the file changed, the template is intact, both phases are clean. So the sample reads the file for directives naming a tracked rule and the check requires none. Suppressing a diagnostic is a legitimate thing to do in real Bicep; it is not the repair turn 2 was asked for.

## Counted, not claimed

Container counts come from `docker ps -a --filter label=maf-sandbox.thread=…` — the labels the backend stamps and `dispose_scope` selects on, with `-a` so a container stopped but not removed still counts.

Every other number is the compiler's or the file store's. The live check reads them back and requires them to agree with each other: a fault the tally calls fixed the compiler must no longer report, and one it calls remaining or introduced the compiler must still report — by rule *and* target, so a swap within one rule cannot hide between them. Two halves of the same output describing different files is the failure that catches.

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

This sample asks more of a model than any other here: turn 1 has to write valid Bicep to a brief, and turn 2 has to read diagnostics, work out what each means for the source, and make the edit. A small local model can author and validate fine and still return nothing useful for the fix turn. The output says exactly what the file looks like afterwards either way.

## One retry, announced

The fix turn is a live model doing open-ended work, so it sometimes does not converge — eight of the nine shipped runs before [#421](https://github.com/sokolaidev/maf-extensions/issues/421) passed, and the ninth reddened a publish that had otherwise succeeded. So the live job runs the two-turn loop **twice at most**, and only when the check exits 3: every failure was the repair itself and every deterministic measurement passed. A container that was not reused, a turn that never reached the sandbox, a file that was never written or never changed, a suppressed rule, a sandbox left behind — those exit 1 and fail on the first attempt, because a second live model cannot mend any of them and re-confirming a broken sandbox costs a container to learn nothing.

The retry annotates the run and the attempt count goes into the job summary, pass or fail. A silent retry is how a check that fails half the time starts reading green, which would cost more than the noise it saves.

## Where this sits

Sample 05 runs this workload once, against a file checked in beside it. Sample 12 shows when a sandbox goes away. This one is the case they leave out — the sandbox that is still there because the conversation is not over, which is what get-or-create was for.
