"""A program inside a sandbox calling back into the host, and what the round trip costs.

Every other sample sends things *in* to a sandbox and takes results *out*.  This one opens the
other direction::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_codeact calls the router        |
                  +------ a host function, dispatched ----------+

Sample 10 is the configuration half: what a host declares before any of this may happen,
answered at attach with no sandbox and no model.  This is the traffic half (#302).  Read 10
first; the acts below use its vocabulary and do not re-teach it.

Two things it exists to measure, neither of which a local backend answers honestly:

- **The program outlives the `exec` that started it.**  ACAS's `exec` is blocking and
  timeout-bounded, and the guest shim blocks on a response file the host can only write while
  the program is still running.  So a dispatch that is answered *at all* proves the launcher
  detached and the supervisor took over.  Nothing below asserts that; it is the precondition
  of act 2 producing any number.
- **What a round trip costs, against what it buys.**  #133 says the trade-off is what the
  feature lives or dies on and should be measured rather than assumed.  Act 4 puts three
  routes side by side: the question answered by a program that dispatches, by a model calling
  the same function and reporting the total alone, and by the same model asked to write its
  working down first.  The third is what keeps the comparison honest — without it the second
  reads as "a model cannot add", and the README shows that is not what is happening.

Running this needs a real Azure subscription and **creates a billable sandbox** — see this
directory's README for the prerequisites and the environment variables.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-acas>=0.9",
#     "maf-sandbox-codeact>=0.5",
#     "maf-sandbox>=0.16",
# ]
# ///

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _scaffold import MEASURED, quoted, require_env_vars, tool_results
from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    HostToolRegistry,
    Identity,
    SandboxRouter,
    SourceIntegrity,
    sandbox_tool,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import make_codeact_tools

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "15-acas-codeact-host-tools"
AGENT_DIR = "order-desk"

#: Sample 14's image, imported into the sandbox group as a disk image. Fully qualified, so no
#: registry variable accompanies it. The transport's launcher is POSIX shell and its shim is
#: Python, so the guest needs `sh`, `nohup` and `python3` — a devcontainer image has all three.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: The host's price list, and the point of the sample: it is not in the sandbox, the sandbox
#: has no egress to go and find it, and no model has seen it. A program that wants these
#: numbers has one way to get them, which is to call back out.
PRICES = {"SKU-A": 41.75, "SKU-B": 12.40, "SKU-C": 3.05}

#: Quantities the task names. Awkward on purpose, in the spirit of sample 03 refusing a number
#: a model could recite: the arithmetic has to be work, or the sample proves nothing about
#: where the answer came from.
ORDER = (("SKU-A", 3), ("SKU-B", 7), ("SKU-C", 2))

#: What the order costs. Computed here, from the same two constants the guest reaches through
#: the tool — so both routes are scored against a truth neither of them produced.
EXPECTED = sum(quantity * PRICES[sku] for sku, quantity in ORDER)

#: The name every route registers the function under, and the name act 2's program calls.
TOOL_NAME = "unit_price"

#: The kind's tool, read for what the program printed rather than for what the model said
#: about it. `tool_results` matches by `call_id`, so this is the framework's record of the
#: sandbox's own stdout — the one line in this sample no model has a hand in.
CODEACT_TOOL = "execute_code"

TASK = (
    "Compute the total cost of this order and report it to two decimals: "
    + ", ".join(f"{quantity} x {sku}" for sku, quantity in ORDER)
    + "."
)

#: Everything the sandbox backend needs. No `ACAS_SANDBOX_REGISTRY`: `CODEACT_IMAGE` is
#: already fully-qualified.
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
)

#: Everything the chat model needs. Auth is `DefaultAzureCredential`, so there is no key
#: here — `az login` is enough.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")

#: Every route gets the same standing order, so act 4 compares the road the answer travelled
#: rather than how hard each side was pushed.
INSTRUCTIONS = (
    "You answer with numbers you computed, never numbers you worked out in your head. "
    f"Prices are not yours to guess: every price must come from the {TOOL_NAME} tool."
)

#: Added to `INSTRUCTIONS` for the third route, and the reason there is a third one. Measured
#: over fifteen runs of this task, a model asked for the total and nothing else answers in one
#: pass with no reasoning tokens and is wrong every time; asked to put the line totals on the
#: page first, it is exact every time. So "the model got it wrong" is not the finding — the
#: finding is that it had nowhere to do the arithmetic, and this route is what separates the
#: two. Labelling the tool's answers `SKU-A=41.75` instead of `41.75` changes nothing, which
#: is how the mis-pairing explanation was ruled out.
SHOW_YOUR_WORKING = (
    " Before you give the total, write out each line as quantity x unit price = line total, "
    "one per line, then add the line totals together and show that addition."
)


class Ledger:
    """What the host was asked, and when.

    A timestamp on both sides of the body rather than one: the interval that means something
    runs from *answering* one call to the *next arriving*, which is a full trip out through the
    response file, the guest, the next request file and back. Timing arrival to arrival would
    fold the host's own work into the number.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._arrived: list[float] = []
        self._answered: list[float] = []

    def arriving(self, sku: str) -> None:
        self.asked.append(sku)
        self._arrived.append(time.perf_counter())

    def answered(self) -> None:
        self._answered.append(time.perf_counter())

    @property
    def round_trips(self) -> list[float]:
        """One entry per consecutive pair, so *n* calls yield *n - 1* of them.

        A value below the supervisor's poll interval is not a fast round trip — it is two calls
        that were outstanding together, which the transport allows and a threaded program
        produces. Nothing here filters those out: a measurement that drops its inconvenient
        samples is not one, and the README says what to read into a small number.
        """
        return [
            self._arrived[index + 1] - self._answered[index]
            for index in range(len(self._answered) - 1)
        ]


def price_of(sku: str, ledger: Ledger) -> float:
    """The one body both routes call, so the comparison is not confounded by two of them."""
    ledger.arriving(sku)
    try:
        return PRICES[sku]
    finally:
        ledger.answered()


def dispatchable(ledger: Ledger) -> Any:
    """The host function as a *dispatchable* tool: stamped, for registration behind the gate.

    `source` is `TRUSTED` because what comes back is the host's own table rather than something
    a model or a network chose. `sink` is `None` — the call carries a SKU out and nothing else,
    and the SKU came from the task. `identity` is `APP`, the application's own authority, which
    is the honest reading of a lookup nobody has to approve; it is not the safe option, only the
    declared one. Sample 10's README is the long version of all three.
    """

    @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
    def unit_price(sku: str) -> float:
        """The unit price for a SKU, from the host's internal price list."""
        return price_of(sku, ledger)

    return unit_price


def directly(ledger: Ledger) -> Any:
    """The same function as an ordinary MAF tool, for act 3.

    No `@sandbox_tool` stamp, and that asymmetry is act 3's subject rather than an oversight:
    the declarations describe *dispatch*, and a tool the model calls itself never crosses a
    sandbox boundary — it is already inside whatever MAF governs.
    """

    @tool
    def unit_price(sku: str) -> float:
        """The unit price for a SKU, from the host's internal price list."""
        return price_of(sku, ledger)

    return unit_price


def agent_for(
    env: dict[str, str],
    credential: DefaultAzureCredential,
    tools: list[Any],
    *,
    working: bool = False,
) -> Agent:
    """One agent shape, built three times, differing only in its tools and its standing order."""
    return Agent(
        client=OpenAIChatClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
            credential=credential,
        ),
        name=AGENT_DIR,
        instructions=INSTRUCTIONS + (SHOW_YOUR_WORKING if working else ""),
        tools=tools,
    )


def carries_the_total(reply: str) -> bool:
    """Whether the reply carries the exact total, as a substring.

    A substring and not a parse, deliberately: `$218.15`, `**218.15**` and `218.15` all say the
    same thing, and a pattern tight enough to reject a wrong answer is loose enough to reject a
    right one dressed differently. What a substring cannot do is accept a *near* miss — which is
    the only failure mode in play, because a model doing this arithmetic itself lands close and
    wrong rather than exact.
    """
    return f"{EXPECTED:.2f}" in reply


def report(route: str, seconds: float, usage: dict[str, Any], calls: int, exact: bool) -> None:
    """One route's numbers, on the two tagged lines the live check reads."""
    tokens = usage.get("total_token_count")
    print(f"{MEASURED}{route}: {calls} call(s), {seconds:.2f}s, {tokens} tokens")
    print(f"{MEASURED}{route}: reply carries {EXPECTED:.2f}: {exact}")


def act_one_what_the_host_wired(ledger: Ledger) -> HostToolRegistry:
    """Registration and the seal, in the shortest form that is still honest.

    Sample 10 spends four acts here and this spends one paragraph, because the contract is not
    what this sample is measuring — but it cannot be skipped either. Nothing is dispatchable
    until it is registered, and reading the aggregate is what seals the registry.
    """
    print("== 1. What the host wired ==\n")

    registry = HostToolRegistry(require_declared=True)
    registry.register(dispatchable(ledger))
    aggregate = registry.aggregate()

    print(f"  registered:            {TOOL_NAME}")
    print(f"  identities the spec carries: {sorted(str(one) for one in aggregate.identities)}")
    print("  Reading the aggregate sealed the registry: a later register() is refused, because")
    print("  this is the moment the surface became a policy the router can match.\n")
    return registry


async def act_two_the_program_calls_out(
    env: dict[str, str],
    credential: DefaultAzureCredential,
    registry: HostToolRegistry,
    ledger: Ledger,
) -> tuple[SandboxRouter, bool, bool]:
    """One turn, on a real microVM, whose program cannot finish without the host."""
    print("== 2. A program that cannot answer without calling out ==\n")

    backend = AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=env["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=env["ACAS_SANDBOX_GROUP"],
        )
    )
    # No `min_isolation`: the default floor is `MICROVM` and this backend meets it, so the
    # outward channel opens at the highest rung the ladder has rather than an opted-down one.
    router = SandboxRouter([backend])
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)

    tools = make_codeact_tools(
        router,
        AGENT_DIR,
        context,
        # The one argument that opens the outward direction. A non-empty registry widens the
        # spec by HOST_TOOLS *and* FILES_OUT — the transport stats and reads its own request
        # files — and both are refused at construction by a backend that cannot serve them.
        host_tools=registry,
        image=CODEACT_IMAGE,
    )
    if not tools:
        print("No sandbox backend: execute_code was not attached.", file=sys.stderr)
        raise SystemExit(2)

    agent = agent_for(env, credential, tools)
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started

    # Two different questions, and keeping them apart is the whole point of this act.
    #
    # `printed` is what the sandbox put on stdout, read from the framework's own record of what
    # the tool returned rather than from anything the model wrote. An interpreter produced it,
    # so it is exact or the program was wrong — there is no third outcome, and it is the only
    # claim here worth failing a release over.
    #
    # `relayed` is whether the model then repeated it. That is a separate act of trust, and it
    # has been observed to fail: a run where the program printed the total and the reply named
    # a different number. Folding the two together would have blamed the sandbox for it.
    printed = tool_results(response, CODEACT_TOOL)
    computed = any(carries_the_total(result) for result in printed)
    relayed = carries_the_total(response.text)

    print(quoted(response.text))
    print()
    print(f"{MEASURED}dispatches: {len(ledger.asked)} across {len(set(ledger.asked))} SKU(s)")
    report(
        "dispatch route", seconds, dict(response.usage_details or {}), len(ledger.asked), relayed
    )
    print(f"{MEASURED}dispatch route: the program printed {EXPECTED:.2f}: {computed}")

    trips = ledger.round_trips
    if trips:
        print(
            f"{MEASURED}round trip: {len(trips)} gap(s), "
            f"min {min(trips):.2f}s, median {median(trips):.2f}s, max {max(trips):.2f}s"
        )
    print()
    return router, computed, relayed


async def _without_a_sandbox(
    env: dict[str, str], credential: DefaultAzureCredential, route: str, *, working: bool
) -> bool:
    """One no-sandbox route: the same function as an ordinary tool, the same question."""
    ledger = Ledger()
    agent = agent_for(env, credential, [directly(ledger)], working=working)
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started

    exact = carries_the_total(response.text)
    print(quoted(response.text))
    print()
    report(route, seconds, dict(response.usage_details or {}), len(ledger.asked), exact)
    print()
    return exact


async def act_three_the_model_calls_it_itself(
    env: dict[str, str], credential: DefaultAzureCredential
) -> tuple[bool, bool]:
    """The same question, the same function, no sandbox — asked two ways.

    Two, because one would licence the wrong conclusion. A single no-sandbox route coming back
    wrong reads as "the model cannot add", and that is not what is happening: asked for the
    total and nothing else it answers in one pass with no reasoning tokens, and asked to put
    the line totals on the page first it is exact. The second route is what stops act 4
    claiming the first thing.
    """
    print("== 3. The same question, answered without a sandbox ==\n")
    print("  Asked twice: once for the total alone, once for the working first.\n")

    one_pass = await _without_a_sandbox(env, credential, "one-pass route", working=False)
    shown = await _without_a_sandbox(env, credential, "shown-working route", working=True)
    return one_pass, shown


def act_four_what_the_round_trip_bought(
    computed: bool, relayed: bool, one_pass: bool, shown: bool
) -> None:
    """The comparison. Three routes, one price table, and what each did with it.

    All three read the same table through the same Python body, so a difference between them is
    never a difference in what the model was told.
    """
    print("== 4. What the round trips bought ==\n")
    print(f"  The order costs {EXPECTED:.2f}. All three routes were given the same prices.\n")
    print(f"{MEASURED}the sandbox computed the exact total:        {computed}")
    print(f"{MEASURED}dispatch route reached the exact total:      {relayed}")
    print(f"{MEASURED}one-pass route reached the exact total:      {one_pass}")
    print(f"{MEASURED}shown-working route reached the exact total: {shown}")
    print()
    print("  Only the first line is a fact about the sandbox. The three below it are facts")
    print("  about a model relaying, guessing or writing down a sum it was handed.\n")
    print("  The one-pass route is much the cheapest and has nowhere to do the arithmetic: no")
    print("  reasoning tokens, and an answer about ten tokens long. The shown-working route")
    print("  buys the same correctness the sandbox does and pays for it in tokens — but it")
    print("  pays into the transcript, where the sum is generated text nothing checked. The")
    print("  dispatch route is the slowest, and the only one where the arithmetic was executed")
    print("  rather than written. A round trip does not buy a cleverer model. It buys a place")
    print("  to compute that is not the model.\n")


async def run() -> int:
    """Wire the stack, run all three routes, and take the sandbox down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    dispatch_ledger = Ledger()
    registry = act_one_what_the_host_wired(dispatch_ledger)

    credential = DefaultAzureCredential()
    router: SandboxRouter | None = None
    try:
        router, computed, relayed = await act_two_the_program_calls_out(
            env, credential, registry, dispatch_ledger
        )
        one_pass, shown = await act_three_the_model_calls_it_itself(env, credential)
        act_four_what_the_round_trip_bought(computed, relayed, one_pass, shown)
    finally:
        if router is not None:
            # Deletes rather than relying on the lifecycle timers — see sample 01's README.
            deleted = await router.dispose_scope(SCOPE, THREAD_ID)
            print(f"{MEASURED}Disposed {deleted} sandbox(es).")
        await credential.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
