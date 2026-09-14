# CodeAct on Hyperlight in DEV/CI and ACAS elsewhere

One Fibonacci task, with the host selecting the sandbox from its environment. The default run asks an agent to write and execute Python; `--smoke` sends a fixed program through the same CodeAct tool without a model. Both check the tool's actual output and clean up before exiting.

This is a **source-only experimental sample** because `maf-sandbox-hyperlight` has not been released. Unlike the numbered PyPI samples, run it from this repository's locked workspace. There is no PEP 723 block or claim that the published packages can run it yet.

## Selection

| Host configuration | Backend | CodeAct contract |
| --- | --- | --- |
| `CI=true`, `CI=1`, or `CI=yes` | Hyperlight, taking precedence over `APP_ENV` | `CodeactRuntime(RUNTIME_INSTRUCTIONS)` |
| `APP_ENV=DEV` or `APP_ENV=CI` | Hyperlight | `RUN_CODE`, no image or file channels |
| Any other nonempty `APP_ENV`, such as `STAGING` or `PROD` | ACAS | Default exec variant, service image `python-3.13` |

Values are case-insensitive and whitespace is stripped. `CI=false`, `CI=0`, `CI=no`, an empty value or an unset variable leaves selection to `APP_ENV`. Other `CI` values are errors. Missing both a true CI signal and a nonempty `APP_ENV` is a configuration error, before any resources are created. Use the exact value `DEV` for development; `DEVELOPMENT` is an other-environment value and selects ACAS.

Only the selected backend is imported and constructed. Selection does not depend on Azure credentials, the model's request, or whether a hypervisor happens to work. An unavailable Hyperlight host fails; it never falls back to a billable ACAS sandbox. Both routes retain the router's default microVM isolation floor and closed guest egress. `Cleanup.RESET` allows Hyperlight to reset between calls; ACAS uses the stronger disposal rung. The final scope purge is followed by backend closure even if execution or purge fails.

## Run from source

From the repository root, using host Python 3.13:

```powershell
uv sync --locked --python 3.13
$env:APP_ENV = "DEV"
uv run --locked python samples/experimental/hyperlight_acas_codeact/agent.py --smoke
```

Hyperlight requires **Windows x86-64 with Windows Hypervisor Platform enabled**. The packaged guest has a reduced Python standard library, no shell or package installation, and no file-transfer or host-tool channels. The program uses only integer arithmetic, which works on both backends. See the [Hyperlight requirements](../../../packages/maf-sandbox-hyperlight/README.md#requirements) for the supported SDK and host configuration.

To run an agent turn, omit `--smoke` and set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL` to an Azure OpenAI deployment supported by `OpenAIChatClient`. Authentication uses `DefaultAzureCredential`, including an `az login` session. The model runs outside the sandbox; using Hyperlight locally does not make model inference local or free.

For ACAS, set `APP_ENV=STAGING` or `APP_ENV=PROD` and configure:

| Variable | Value |
| --- | --- |
| `ACAS_SANDBOX_ENDPOINT` | Sandbox group's data-plane endpoint |
| `ACAS_SANDBOX_SUBSCRIPTION_ID` | Subscription holding the group |
| `ACAS_SANDBOX_RESOURCE_GROUP` | Resource group holding the group |
| `ACAS_SANDBOX_GROUP` | Sandbox group name |

Use an existing sandbox group and `DefaultAzureCredential` with permission to access it; see [sample 03](../../03_acas_codeact/README.md#prerequisites). This route **creates a billable sandbox**, including in smoke mode. The prebuilt `python-3.13` image needs no registry variable. If `CI=true` is set, it still selects Hyperlight; set `CI=false` explicitly to exercise ACAS from a CI process.

## CI

A live smoke job must run on a Windows x86-64 runner with WHP available to the job account. The repository's ordinary hosted Windows job tests worker safety with live Hyperlight disabled; it does not establish that its runner can execute a microVM. This sample adds no requirement for WHP to ordinary PR CI.

On a prepared WHP runner, the complete smoke step is:

```powershell
$env:CI = "true"
uv sync --locked --python 3.13
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --locked python samples/experimental/hyperlight_acas_codeact/agent.py --smoke
exit $LASTEXITCODE
```

This step needs no Azure configuration, credentials, or model. It fails on a missing hypervisor, a failed tool result, a wrong integer, or incomplete cleanup. Run only one Hyperlight host process per machine at a time, including other live tests. Selection and cleanup are also covered by portable repository tests, which use fakes and do not claim live hypervisor or Azure execution.

Successful output includes `[measured] Backend: hyperlight` (or `acas`), a CodeAct result containing `stdout:` followed by `354224848179261915075`, and a final disposal count. That final count can be zero when per-call cleanup already disposed the sandbox.
