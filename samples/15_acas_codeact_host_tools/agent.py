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
  feature lives or dies on and should be measured rather than assumed.  Act 4 puts the two
  routes side by side: the same question answered by a program that dispatches, and answered
  by a model calling the same function itself.

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

from _scaffold import MEASURED, quoted, require_env_vars
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

#: The name both routes register the function under, and the name act 2's program calls.
TOOL_NAME = "unit_price"

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

#: Both routes get the same standing order, so act 4 compares the road the answer travelled
#: rather than how hard each side was pushed.
INSTRUCTIONS = (
    "You answer with numbers you computed, never numbers you worked out in your head. "
    f"Prices are not yours to guess: every price must come from the {TOOL_NAME} tool."
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


def agent_for(env: dict[str, str], credential: DefaultAzureCredential, tools: list[Any]) -> Agent:
    """One agent shape, built twice, differing only in what is in `tools`."""
    return Agent(
        client=OpenAIChatClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
            credential=credential,
        ),
        name=AGENT_DIR,
        instructions=INSTRUCTIONS,
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
) -> tuple[SandboxRouter, bool]:
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

    exact = carries_the_total(response.text)
    print(quoted(response.text))
    print()
    print(f"{MEASURED}dispatches: {len(ledger.asked)} across {len(set(ledger.asked))} SKU(s)")
    report("dispatch route", seconds, dict(response.usage_details or {}), len(ledger.asked), exact)

    trips = ledger.round_trips
    if trips:
        print(
            f"{MEASURED}round trip: {len(trips)} gap(s), "
            f"min {min(trips):.2f}s, median {median(trips):.2f}s, max {max(trips):.2f}s"
        )
    print()
    return router, exact


async def act_three_the_model_calls_it_itself(
    env: dict[str, str], credential: DefaultAzureCredential, ledger: Ledger
) -> bool:
    """The same question, the same function, and no sandbox anywhere."""
    print("== 3. The same question, answered without a sandbox ==\n")

    agent = agent_for(env, credential, [directly(ledger)])
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started

    exact = carries_the_total(response.text)
    print(quoted(response.text))
    print()
    report("direct route", seconds, dict(response.usage_details or {}), len(ledger.asked), exact)
    print()
    return exact


def act_four_what_the_round_trip_bought(dispatched: bool, direct: bool) -> None:
    """The comparison, stated as what each route did with the same prices.

    Both routes read the same table through the same body, so a difference between them is not
    a difference in what the model knew. It is a difference in what did the arithmetic.
    """
    print("== 4. What the round trips bought ==\n")
    print(f"  The order costs {EXPECTED:.2f}. Both routes were given the same prices.\n")
    print(f"{MEASURED}dispatch route reached the exact total: {dispatched}")
    print(f"{MEASURED}direct route reached the exact total:   {direct}")
    print()
    print("  The dispatch route is slower and costs more tokens — a program that calls out")
    print("  pays a round trip per call, and the code it wrote is in the transcript. What it")
    print("  buys is arithmetic done by an interpreter. The direct route pays for neither and")
    print("  hands the sum to the model, which is the one step it has no way to check.\n")


async def run() -> int:
    """Wire the stack, run both routes, and take the sandbox down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    dispatch_ledger = Ledger()
    direct_ledger = Ledger()
    registry = act_one_what_the_host_wired(dispatch_ledger)

    credential = DefaultAzureCredential()
    router: SandboxRouter | None = None
    try:
        router, dispatched = await act_two_the_program_calls_out(
            env, credential, registry, dispatch_ledger
        )
        direct = await act_three_the_model_calls_it_itself(env, credential, direct_ledger)
        act_four_what_the_round_trip_bought(dispatched, direct)
    finally:
        if router is not None:
            # Deletes rather than relying on the lifecycle timers — see sample 01's README.
            deleted = await router.dispose_scope(SCOPE, THREAD_ID)
            print(f"{MEASURED}Disposed {deleted} sandbox(es).")
        await credential.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
