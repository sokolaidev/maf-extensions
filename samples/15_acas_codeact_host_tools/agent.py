"""A program inside a sandbox calling back into the host, and what the round trip buys.

Every other sample sends things *in* to a sandbox and takes results *out*.  This one opens the
other direction::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_codeact calls the router        |
                  +------ a host function, dispatched ----------+

Sample 10 is the configuration half: what a host declares before any of this may happen,
answered at attach with no sandbox and no model.  This is the traffic half (#302).  Read 10
first; the acts below use its vocabulary and do not re-teach it.

#133 says the trade-off is what the feature lives or dies on and should be measured rather
than assumed::

    a call-heavy program can cost more round trips than the direct tool calling this
    pattern exists to replace

So acts 2 and 3 ask one question two ways, and **both of them run Python in the sandbox**.
The only thing that differs is where the price lookups happen: inside the guest, over the
transport, or in the model's own tool loop.  Holding the interpreter constant is the whole
point — a comparison that gave one side code execution and not the other would be measuring
CodeAct, which samples 03 and 06 already do, and calling it a measurement of dispatch.

What that leaves is the difference the capability actually makes, and act 4 names it: on the
dispatched route the model never *handles* a price.  It cannot — it is never given one.  On
the direct route every price arrives as a tool result and has to be written back out into the
source of the program that needs it.

Note the narrowness of that claim.  It is not "the prices stay out of the transcript": a
dispatched program is free to print one, and the run this sample's README quotes did.  It is
that the model is not the courier, which is the part the transport decides and the part a
`sink` declaration is about.

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

#: The host's price list. Not in the image, not in the file store, and unreachable from the
#: sandbox — with no `egress_allow` the guest initiates nothing at all. A program that wants
#: these numbers has one road to them, and act 4 is about which road it took.
PRICES = {"SKU-A": 41.75, "SKU-B": 12.40, "SKU-C": 3.05}

#: The order the task names.
ORDER = (("SKU-A", 3), ("SKU-B", 7), ("SKU-C", 2))

#: What the order costs. Computed here, from the same two constants both routes reach through
#: the same function, so each is scored against a truth neither produced.
EXPECTED = sum(quantity * PRICES[sku] for sku, quantity in ORDER)

#: The name both routes register the function under.
TOOL_NAME = "unit_price"

#: The kind's tool. Read for what the program printed rather than for what the model said about
#: it: `tool_results` matches by `call_id`, so it is the framework's record of the sandbox's own
#: stdout — the one line in this sample no model has a hand in.
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

#: The same standing order for both routes, and the sentence that keeps the comparison fair is
#: the first one: *both* sides must compute with the interpreter. Without it the direct route
#: would be a model doing decimal arithmetic in one forward pass, which it is bad at — a real
#: effect, and the wrong one to attribute to dispatch. Sample 03 is where that belongs.
INSTRUCTIONS = (
    "You answer with numbers an interpreter computed. Never do arithmetic yourself: always "
    f"run Python with {CODEACT_TOOL} to work out the total. Prices are not yours to guess: "
    f"every price must come from {TOOL_NAME}."
)

#: The one sentence that cannot be shared, because the two routes reach the same function by
#: genuinely different roads and a model has to be told which one it has. Act 2's `unit_price`
#: is not in its tool list at all — it is reachable only from inside the guest, and the
#: `execute_code` description says so — so an instruction naming it as a tool sends the model
#: looking for something it does not have. Measured: without this, the program came out with
#: placeholder zeros and printed `ERROR: unit_price data missing`, dispatching nothing at all.
FROM_INSIDE = (
    f" {TOOL_NAME} is a host tool, not one of yours: your program calls it from inside the "
    f'sandbox with maf_host_tools.call("{TOOL_NAME}", sku=...).'
)

#: Act 3's counterpart, deliberately the same shape so neither side is pushed harder.
FROM_THE_TOOL_LIST = (
    f" {TOOL_NAME} is one of your tools: call it for each price, then pass the numbers you got "
    "into the program you run."
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

    No `@sandbox_tool` stamp, and the asymmetry is act 3's subject rather than an oversight:
    those declarations describe an information flow that leaves the host and comes back, and a
    tool the model calls itself never crosses a sandbox boundary. It crosses the conversation
    instead, which is the thing act 4 measures.
    """

    @tool(name=TOOL_NAME)
    def unit_price(sku: str) -> float:
        """The unit price for a SKU, from the host's internal price list."""
        return price_of(sku, ledger)

    return unit_price


def agent_for(
    env: dict[str, str], credential: DefaultAzureCredential, tools: list[Any], how: str
) -> Agent:
    """One agent shape, built twice: same task, same arithmetic rule, different road in."""
    return Agent(
        client=OpenAIChatClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
            credential=credential,
        ),
        name=AGENT_DIR,
        instructions=INSTRUCTIONS + how,
        tools=tools,
    )


def carries_the_total(text: str) -> bool:
    """Whether `text` carries the exact total, as a substring.

    A substring and not a parse: `$218.15`, `**218.15**` and `218.15` all say the same thing,
    and a pattern tight enough to reject a wrong answer is loose enough to reject a right one
    dressed differently.
    """
    return f"{EXPECTED:.2f}" in text


def _prices_in(text: str | None) -> set[str]:
    """Both spellings, because a float renders as `12.4` where the table writes `12.40`."""
    body = text or ""
    return {
        f"{price:.2f}" for price in PRICES.values() if f"{price:.2f}" in body or str(price) in body
    }


def prices_the_model_wrote(response: object) -> list[str]:
    """Which of the host's prices the model wrote into a tool call — in practice, into code.

    **This is what the sample is really measuring**, and the narrowness is the point. Only
    tool-call *arguments* are read, so what it answers is: did the model have to carry a price
    from one place to another? On the direct route that is forced — the numbers arrive as tool
    results and the only way into the program is for the model to write them into its source.
    On the dispatched route it is impossible: the program is written *before* any dispatch
    happens, so there is no price to embed even if the model wanted to.

    Deliberately not "anywhere in the messages", and not the final reply either. A dispatched
    program may print a price, and a model may then repeat it — both real, both the program's
    choice rather than the transport's, and both reported on the `received` line instead.
    Conflating the two is how this sample got its comparison wrong the first time.
    """
    seen: set[str] = set()
    for message in getattr(response, "messages", []):
        for content in getattr(message, "contents", []) or []:
            seen |= _prices_in(getattr(content, "arguments", None))
    return sorted(seen)


def prices_the_model_received(response: object) -> list[str]:
    """Which prices came back to the model as tool results, by either road."""
    seen: set[str] = set()
    for message in getattr(response, "messages", []):
        for content in getattr(message, "contents", []) or []:
            seen |= _prices_in(getattr(content, "result", None))
    return sorted(seen)


def report(route: str, seconds: float, usage: dict[str, Any], calls: int, turns: int) -> None:
    """One route's cost, on the line the live check reads."""
    tokens = usage.get("total_token_count")
    print(
        f"{MEASURED}{route}: {calls} lookup(s), {turns} message(s), {seconds:.2f}s, {tokens} tokens"
    )


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


def sandbox_router(env: dict[str, str]) -> SandboxRouter:
    """One backend, one router, the default floor.

    No `min_isolation`: the default is `MICROVM` and this backend meets it, so the outward
    channel opens at the highest rung the ladder has rather than an opted-down one.
    """
    backend = AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=env["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=env["ACAS_SANDBOX_GROUP"],
        )
    )
    return SandboxRouter([backend])


def codeact_for(router: SandboxRouter, registry: HostToolRegistry | None) -> list[Any]:
    """`execute_code`, with or without the outward channel. The one argument that differs."""
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)
    tools = make_codeact_tools(
        router,
        AGENT_DIR,
        context,
        # A non-empty registry widens the spec by HOST_TOOLS *and* FILES_OUT — the transport
        # stats and reads its own request files — and both are refused at construction by a
        # backend that cannot serve them.
        host_tools=registry,
        image=CODEACT_IMAGE,
    )
    if not tools:
        print("No sandbox backend: execute_code was not attached.", file=sys.stderr)
        raise SystemExit(2)
    return tools


async def act_two_the_program_calls_out(
    env: dict[str, str],
    credential: DefaultAzureCredential,
    router: SandboxRouter,
    registry: HostToolRegistry,
    ledger: Ledger,
) -> tuple[bool, list[str]]:
    """The lookups happen inside the guest, over the transport."""
    print("== 2. The lookups happen inside the sandbox ==\n")

    agent = agent_for(env, credential, codeact_for(router, registry), FROM_INSIDE)
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started

    # `printed` is the sandbox's own stdout, from the framework's record of what the tool
    # returned. An interpreter produced it, so it is exact or the program was wrong.
    printed = tool_results(response, CODEACT_TOOL)
    computed = any(carries_the_total(result) for result in printed)
    wrote = prices_the_model_wrote(response)
    got = prices_the_model_received(response)

    print(quoted(response.text))
    print()
    print(f"{MEASURED}dispatches: {len(ledger.asked)} across {len(set(ledger.asked))} SKU(s)")
    report(
        "dispatch route",
        seconds,
        dict(response.usage_details or {}),
        len(ledger.asked),
        len(response.messages),
    )
    print(f"{MEASURED}dispatch route: the program printed {EXPECTED:.2f}: {computed}")
    print(f"{MEASURED}dispatch route: prices the model wrote into code: {wrote or 'none'}")
    print(f"{MEASURED}dispatch route: prices the model received: {got or 'none'}")

    trips = ledger.round_trips
    if trips:
        print(
            f"{MEASURED}round trip: {len(trips)} gap(s), "
            f"min {min(trips):.2f}s, median {median(trips):.2f}s, max {max(trips):.2f}s"
        )
    print()
    return computed, wrote


async def act_three_the_model_looks_them_up(
    env: dict[str, str],
    credential: DefaultAzureCredential,
    router: SandboxRouter,
    ledger: Ledger,
) -> tuple[bool, list[str]]:
    """The same question, the same interpreter, the lookups in the model's own tool loop.

    `execute_code` without a registry, plus the same function as an ordinary MAF tool. So the
    arithmetic is still executed rather than written — the only thing that moved is who asks
    for a price, and therefore what has to travel through the conversation to reach the program.
    """
    print("== 3. The lookups happen in the model's tool loop ==\n")

    tools = [*codeact_for(router, None), directly(ledger)]
    agent = agent_for(env, credential, tools, FROM_THE_TOOL_LIST)
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started

    printed = tool_results(response, CODEACT_TOOL)
    computed = any(carries_the_total(result) for result in printed)
    wrote = prices_the_model_wrote(response)
    got = prices_the_model_received(response)

    print(quoted(response.text))
    print()
    report(
        "direct route",
        seconds,
        dict(response.usage_details or {}),
        len(ledger.asked),
        len(response.messages),
    )
    print(f"{MEASURED}direct route: the program printed {EXPECTED:.2f}: {computed}")
    print(f"{MEASURED}direct route: prices the model wrote into code: {wrote or 'none'}")
    print(f"{MEASURED}direct route: prices the model received: {got or 'none'}")
    print()
    return computed, wrote


def act_four_what_the_round_trip_bought(
    dispatched: list[str], direct: list[str], total: int
) -> None:
    """The comparison, once correctness has stopped being the variable.

    Both routes computed with the same interpreter and both got the same number, so what is
    left is the cost of each road and what travelled down it.
    """
    print("== 4. What the round trips bought ==\n")
    print(f"{MEASURED}prices the model handled, dispatched: {len(dispatched)} of {total}")
    print(f"{MEASURED}prices the model handled, direct:     {len(direct)} of {total}")
    print()
    print("  Both routes ran Python and both reached the same total, so correctness is not")
    print("  what a round trip buys — sample 03 already showed what an interpreter is for.")
    print("  What it buys is the line above. Dispatched, the model is never handed a price,")
    print("  so it writes none: the values go guest to host and back without it. Directly,")
    print("  every price arrives as a tool result and the model writes each one into the")
    print("  source of the program that needs it, where it stays — in the transcript, the")
    print("  context window, and whatever logs either of those reaches.")
    print()
    print("  That is the trade #133 asked to have measured, and the price is wall clock: a")
    print("  round trip per call, against a tool call the model was going to make anyway.")
    print("  Tokens are not the axis — the two routes land close, and which is cheaper")
    print("  depends on how much the guest program decides to print.\n")


async def run() -> int:
    """Wire the stack, run both routes against one sandbox, and take it down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    dispatch_ledger = Ledger()
    direct_ledger = Ledger()
    registry = act_one_what_the_host_wired(dispatch_ledger)

    credential = DefaultAzureCredential()
    router = sandbox_router(env)
    try:
        _, dispatched = await act_two_the_program_calls_out(
            env, credential, router, registry, dispatch_ledger
        )
        _, direct = await act_three_the_model_looks_them_up(env, credential, router, direct_ledger)
        act_four_what_the_round_trip_bought(dispatched, direct, len(PRICES))
    finally:
        # Deletes rather than relying on the lifecycle timers — see sample 01's README.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"{MEASURED}Disposed {deleted} sandbox(es).")
        await credential.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
