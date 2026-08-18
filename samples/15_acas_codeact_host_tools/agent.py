"""A program inside a sandbox calling back into the host, and what the round trips buy.

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

    Each dispatch is at minimum one round trip — on a remote backend, an HTTP call — so a
    call-heavy program can cost more round trips than the direct tool calling this pattern
    exists to replace.

So the workload is deliberately call-heavy *and* deep.  Three tables — states, stores, sales —
and a question that cannot be answered without walking them in order::

    state name -> state id -> store ids -> sales rows -> product names

Four stages, a dozen lookups.  Acts 2 and 3 answer it twice, and **both run Python in the
sandbox**: the only thing that differs is where the lookups happen.  Holding the interpreter
constant is what keeps this a measurement of dispatch rather than of CodeAct, which samples 03
and 06 already cover.

What that isolates is the thing the capability decides, and act 4 names it: direct tool calling
batches *within* a stage but never across one, so it pays a model round trip per stage and
every value it fetched crosses the conversation.  Dispatch pays a transport round trip per
call — serially, always (#439) — and the model handles nothing.

Act 5 is what the runs left in the guest, which #302 asks for and which is only half
answerable: a fresh directory per run is real, and cleaning it up is #438.

Running this needs a real Azure subscription and **creates two billable sandboxes**, one per
route — see this directory's README for the prerequisites, the environment variables, and why
the routes cannot share one.
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
    CALLS_DIRECTORY,
    HostToolRegistry,
    Identity,
    SandboxKey,
    SandboxRouter,
    SourceIntegrity,
    sandbox_tool,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import codeact_sandbox_spec, make_codeact_tools

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
AGENT_DIR = "analyst"

#: A sandbox each, and the reason is act 5. Nothing deletes a run's transport files, so the
#: dispatched route's responses — every state id, store list, sales row and product name —
#: stay readable on the guest filesystem for anything that runs there afterwards. Sharing one
#: sandbox would give the direct route's program a second road to the same data that this
#: sample never measures, and the comparison rests on there being only one.
#:
#: Not fixable by cleaning up between the two: there is no way to delete a guest file (#438),
#: which is the same gap act 5 reports. Two keys is what isolation looks like when deletion is
#: not on the table.
DISPATCH_THREAD = "15-acas-codeact-host-tools-dispatch"
DIRECT_THREAD = "15-acas-codeact-host-tools-direct"

#: Sample 14's image, available to the sandbox group as a disk image. The transport's launcher
#: is POSIX shell and its shim is Python, so the guest needs `sh`, `nohup`, `mkdir`, `mv`,
#: `printf` and `python3` — a distroless or Windows image cannot serve this whatever it
#: declares.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

# --------------------------------------------------------------------------------------
# Three tables, as a real deployment would have them: in the host, behind a lookup, and
# nowhere the sandbox can reach. With no `egress_allow` the guest initiates nothing at all,
# so a program that wants any of this has exactly one road to it.
# --------------------------------------------------------------------------------------

STATES = {"Washington": "ST-WA", "Oregon": "ST-OR"}
STORES = {"ST-WA": ["STO-101", "STO-102", "STO-103"], "ST-OR": ["STO-201", "STO-202"]}
PRODUCTS = {"PRD-1": "Widget", "PRD-2": "Gasket", "PRD-3": "Flange"}
SALES = {
    "STO-101": [["PRD-1", 1240.50], ["PRD-2", 310.25], ["PRD-3", 88.10]],
    "STO-102": [["PRD-1", 655.75], ["PRD-3", 1002.40]],
    "STO-103": [["PRD-2", 47.90], ["PRD-3", 219.65]],
    "STO-201": [["PRD-1", 980.00], ["PRD-2", 1150.35]],
    "STO-202": [["PRD-1", 12.05], ["PRD-2", 640.80], ["PRD-3", 731.15]],
}

#: What the walk costs at best, and what it costs written naively. The difference is the
#: product names: a program that caches them asks three times, one that looks one up per sales
#: row asks twelve. Both are reasonable programs and the host does not get to choose which the
#: model writes.
MINIMUM_LOOKUPS = len(STATES) * 2 + len(SALES) + len(PRODUCTS)
NAIVE_LOOKUPS = len(STATES) * 2 + len(SALES) + sum(len(rows) for rows in SALES.values())

#: The registry's default is 16 a run and this walk does not fit, so a call-heavy host has to
#: raise it deliberately. Set above the naive figure rather than at it: the model writes the
#: program, and one that re-reads a product name it already has costs more than the arithmetic
#: predicts. A program that exhausts the budget returns a partial answer, not an error.
DISPATCH_CAP = NAIVE_LOOKUPS + 11

#: The four lookups, and the order they have to happen in. Naming the stages here rather than
#: leaving them implicit, because the count is the measurement: a stage is what direct tool
#: calling pays a model round trip for, and what dispatch does not.
STAGES = ("state_id", "stores_in_state", "store_sales", "product_name")

#: A served call leaves three files: the id its caller claimed, the request, and the answer.
#: Counting the answers separately is what makes the total legible — a bare file count reads
#: as three times the traffic there was.
_RESPONSE_SUFFIX = ".response.json"

#: The two roads, spelled once.
DISPATCH_ROUTE = "dispatch route"
DIRECT_ROUTE = "direct route"

#: Every sales figure, in both spellings a float renders as, and the distinct set behind them.
#: Act 4 reports how many the model had to write down against how many there are, so a run
#: that carried some but not all is visible rather than rounded to "some".
AMOUNTS = {f"{amount:.2f}" for rows in SALES.values() for _, amount in rows} | {
    str(amount) for rows in SALES.values() for _, amount in rows
}
AMOUNTS_EXPECTED = {f"{amount:.2f}" for rows in SALES.values() for _, amount in rows}


def truth() -> dict[str, dict[str, float]]:
    """The answer, computed host-side from the same three tables both routes reach.

    Neither route had a hand in this, which is what makes it a check rather than a comparison
    of two guesses.
    """
    summary: dict[str, dict[str, float]] = {}
    for state, state_id in STATES.items():
        per_product: dict[str, float] = {}
        for store in STORES[state_id]:
            for product_id, amount in SALES[store]:
                name = PRODUCTS[product_id]
                per_product[name] = round(per_product.get(name, 0.0) + amount, 2)
        summary[state] = per_product
    return summary


TRUTH = truth()
STATE_TOTALS = {state: round(sum(rows.values()), 2) for state, rows in TRUTH.items()}

TASK = (
    "Produce a summary table of total sales by product for these two states: "
    + " and ".join(STATES)
    + ". One row per state and product, using the product name, and a total line per state."
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

#: Shared by both routes, and the first sentence is what keeps the comparison fair: *both*
#: sides compute with the interpreter. Without it the direct route would be a model doing
#: decimal arithmetic in one forward pass, which it is bad at — a real effect, and the wrong
#: one to attribute to dispatch. Sample 03 is where that belongs.
INSTRUCTIONS = (
    "You answer with numbers an interpreter computed. Never add anything up yourself: always "
    "run Python with execute_code for the arithmetic. The data is not yours to guess: every "
    "id, store list, sales row and product name must come from the lookups."
)

#: The sentence that cannot be shared, because the two routes reach the same four functions by
#: genuinely different roads: act 2's lookups are not in the model's tool list at all.
#:
#: The last clause is load-bearing. **The pattern pays off only when one program owns the whole
#: walk** — a model given the sandbox and no such instruction will use it as a REPL, fetching
#: in one call and computing in the next, which puts it back in the middle of the data and
#: costs a model round trip per step.
FROM_INSIDE = (
    " Those four lookups are host tools, not yours. Your program calls them from inside the "
    'sandbox with maf_host_tools.call("state_id", state_name=...), then "stores_in_state" '
    'with state_id=..., then "store_sales" with store_id=..., then "product_name" with '
    "product_id=... Write ONE program that does the whole walk and prints the finished "
    "table. Do not run a program to fetch data and then a second program to use it: the "
    "point is that your program can fetch what it needs while it runs."
)

#: Act 3's counterpart, the same shape so neither side is pushed harder.
FROM_THE_TOOL_LIST = (
    " state_id, stores_in_state, store_sales and product_name are your own tools: call them "
    "to gather the data, then pass what you got into the program you run."
)


class Ledger:
    """What the host was asked, and when.

    A timestamp on both sides of the body rather than one: the interval that means something
    runs from *answering* one call to the *next arriving*, which is a full trip out through the
    response file, the guest, the next request file and back.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._arrived: list[float] = []
        self._answered: list[float] = []

    def arriving(self, what: str) -> None:
        self.asked.append(what)
        self._arrived.append(time.perf_counter())

    def answered(self) -> None:
        self._answered.append(time.perf_counter())

    @property
    def round_trips(self) -> list[float]:
        """One entry per consecutive pair, so *n* calls yield *n - 1* of them."""
        return [
            self._arrived[index + 1] - self._answered[index]
            for index in range(len(self._answered) - 1)
        ]


def build(stamp: Any, ledger: Ledger) -> list[Any]:
    """The four lookups, stamped for dispatch or wrapped as ordinary MAF tools.

    One body per lookup, shared by both routes, so a difference between the acts is never a
    difference in what the function did. `HostToolRegistry.register` keys on `__name__`, so
    these have to be named as the guest calls them — a factory renaming them would register a
    surface the program cannot reach.

    The declarations are the same on both: `TRUSTED` because the answers are the host's own
    tables, `sink=None` because a lookup carries an id out and nothing else, and `APP` because
    this is the application's own authority. Sample 10's README is the long version.
    """

    @stamp
    def state_id(state_name: str) -> str:
        """The id of a state, by name."""
        ledger.arriving(f"state_id({state_name})")
        try:
            return STATES[state_name]
        finally:
            ledger.answered()

    @stamp
    def stores_in_state(state_id: str) -> list:
        """The store ids in a state."""
        ledger.arriving(f"stores_in_state({state_id})")
        try:
            return STORES[state_id]
        finally:
            ledger.answered()

    @stamp
    def store_sales(store_id: str) -> list:
        """Sales rows for a store, as [product_id, amount] pairs."""
        ledger.arriving(f"store_sales({store_id})")
        try:
            return SALES[store_id]
        finally:
            ledger.answered()

    @stamp
    def product_name(product_id: str) -> str:
        """The display name of a product."""
        ledger.arriving(f"product_name({product_id})")
        try:
            return PRODUCTS[product_id]
        finally:
            ledger.answered()

    return [state_id, stores_in_state, store_sales, product_name]


def dispatchable(ledger: Ledger) -> list[Any]:
    """The four lookups as *dispatchable* tools, for registration behind the gate."""
    return build(
        sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP), ledger
    )


def ordinary(ledger: Ledger) -> list[Any]:
    """No `@sandbox_tool` stamp, and the asymmetry is act 3's subject rather than an oversight.

    Those declarations describe an information flow that leaves the host and comes back. A tool
    the model calls itself never crosses a sandbox boundary — it crosses the conversation
    instead, which is what act 4 counts.
    """
    return build(tool, ledger)


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


def calls_per_message(response: object) -> list[int]:
    """Tool calls grouped by the assistant message that asked for them.

    **This is the structural measurement.** Every entry is one *tool-calling round*: the turn
    stops there, the framework runs whatever was asked for, and the model is invoked again. A
    route batching five lookups into one entry paid one round for five; one needing five
    entries paid five.

    Rounds, not model invocations. A message with no tool call is not an entry, and the last
    message always is one — the model writes the answer after the final tool result — so each
    route is invoked once more than this counts. Both pay that extra invocation exactly once,
    which is why the *difference* between the routes is the same either way and the absolute
    figure is not.
    """
    grouped = []
    for message in getattr(response, "messages", []):
        asked = sum(
            1
            for content in getattr(message, "contents", []) or []
            if getattr(content, "type", None) == "function_call"
        )
        if asked:
            grouped.append(asked)
    return grouped


def amounts_the_model_wrote(response: object) -> int:
    """How many distinct sales figures the model itself put into a tool call.

    Tool-call *arguments* only, not the whole transcript, and the narrowness is the point: what
    it answers is whether the model had to carry a value from one place to another. On the
    direct route that is forced — the figures arrive as tool results and the only way into the
    program is for the model to write them into its source. On the dispatched route it is
    impossible, because the program is written before any dispatch can answer.

    A dispatched program may still *print* a figure, and then it is in the transcript by that
    program's choice rather than by the transport's design. Different claim, different line.
    """
    seen: set[str] = set()
    for message in getattr(response, "messages", []):
        for content in getattr(message, "contents", []) or []:
            body = str(getattr(content, "arguments", "") or "")
            seen |= {amount for amount in AMOUNTS if amount in body}
    return len({amount.rstrip("0").rstrip(".") for amount in seen})


def totals_in(text: str) -> int:
    """How many of the state totals the guest program actually printed."""
    return sum(
        1 for total in STATE_TOTALS.values() if f"{total:.2f}" in text or f"{total:,.2f}" in text
    )


def report(
    route: str, seconds: float, usage: dict[str, Any], ledger: Ledger, response: object
) -> int:
    """One route's numbers, on the tagged lines the live check reads."""
    grouped = calls_per_message(response)
    printed = "\n".join(tool_results(response, "execute_code"))
    print(
        f"{MEASURED}{route}: {len(ledger.asked)} lookup(s) over {len(grouped)} "
        f"tool-calling round(s)"
    )
    print(f"{MEASURED}{route}: tool calls per round: {grouped}")
    print(
        f"{MEASURED}{route}: {seconds:.2f}s, {usage.get('total_token_count')} tokens "
        f"(in {usage.get('input_token_count')}, cached {usage.get('cache_read_input_token_count')}, "
        f"out {usage.get('output_token_count')})"
    )
    print(
        f"{MEASURED}{route}: state totals the program printed: {totals_in(printed)} of {len(STATE_TOTALS)}"
    )
    carried = amounts_the_model_wrote(response)
    print(
        f"{MEASURED}{route}: sales figures the model wrote into code: "
        f"{carried} of {len(AMOUNTS_EXPECTED)}"
    )
    return carried


def act_one_what_the_host_wired(ledger: Ledger) -> HostToolRegistry:
    """Registration and the seal, in the shortest form that is still honest."""
    print("== 1. What the host wired ==\n")
    registry = HostToolRegistry(require_declared=True, max_dispatches_per_run=DISPATCH_CAP)
    for lookup in dispatchable(ledger):
        registry.register(lookup)
    aggregate = registry.aggregate()
    print(f"  registered:                  {', '.join(STAGES)}")
    print(
        f"{MEASURED}dispatch cap for the run: {DISPATCH_CAP} "
        f"(the walk needs {MINIMUM_LOOKUPS} at best, {NAIVE_LOOKUPS} written naively)"
    )
    print(f"  identities the spec carries: {sorted(str(one) for one in aggregate.identities)}")
    print("  Reading the aggregate sealed the registry: a later register() is refused, because")
    print("  this is the moment the surface became a policy the router can match.\n")
    return registry


def codeact_for(router: SandboxRouter, registry: HostToolRegistry | None, thread: str) -> list[Any]:
    """`execute_code`, with or without the outward channel, keyed to one route's sandbox."""
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: thread)
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


async def one_route(
    heading: str,
    route: str,
    env: dict[str, str],
    credential: DefaultAzureCredential,
    tools: list[Any],
    how: str,
    ledger: Ledger,
) -> int:
    """One route, start to finish: run the turn, then publish what it cost."""
    print(f"== {heading} ==\n")
    agent = agent_for(env, credential, tools, how)
    started = time.perf_counter()
    response = await agent.run(TASK)
    seconds = time.perf_counter() - started
    print(quoted(response.text))
    print()
    carried = report(route, seconds, dict(response.usage_details or {}), ledger, response)
    # Only the dispatched route has round trips to report. The direct route's lookups run in
    # the host process between two model turns, so the same arithmetic yields microseconds, and
    # printing that under the same name would invite a reader to compare two different things.
    trips = ledger.round_trips
    if trips and route == DISPATCH_ROUTE:
        print(
            f"{MEASURED}{route}: round trip: {len(trips)} gap(s), min {min(trips):.2f}s, "
            f"median {median(trips):.2f}s, max {max(trips):.2f}s"
        )
    print()
    return carried


def act_four_what_the_round_trips_bought(dispatched: int, direct: int) -> None:
    """The comparison, once correctness has stopped being the variable."""
    print("== 4. What the round trips bought ==\n")
    total = len(AMOUNTS_EXPECTED)
    print(f"{MEASURED}sales figures the model handled, dispatched: {dispatched} of {total}")
    print(f"{MEASURED}sales figures the model handled, direct:     {direct} of {total}")
    print()
    print("  Both routes ran Python and both reached the same table, so correctness is not what")
    print("  a round trip buys — sample 03 already showed what an interpreter is for.")
    print()
    print("  Direct tool calling batches within a stage and never across one, so it pays a model")
    print("  round trip per stage of the walk, and every figure it fetched had to be written back")
    print("  into the program by the model. Those values are in the transcript, the context")
    print("  window, and whatever logs either of them reaches — and they stay there, turn after")
    print("  turn, which is a ceiling long before it is a bill.")
    print()
    print("  Dispatch pays a transport round trip per call instead, serially and with no batching")
    print("  available at any layer (#439), and the model handles nothing. That is the trade:")
    print("  wall clock, which is spent per run, against context, which accumulates.\n")


async def _what_one_sandbox_holds(
    router: SandboxRouter, thread: str, registry: HostToolRegistry | None
) -> tuple[int, int, int, int]:
    """Run directories, how many dispatched, and the files those left, for one route's sandbox.

    Acquiring returns the *same warm sandbox* the route used — same key, same spec — which is
    the point of the act: the runs are still in it.
    """
    spec = codeact_sandbox_spec(image=CODEACT_IMAGE, host_tools=registry)
    sandbox = await router.acquire(SandboxKey(SCOPE, thread, AGENT_DIR), spec)
    runs = await sandbox.list_dir(".", working_directory=spec.work_dir)
    directories = sorted(entry.path.rstrip("/").split("/")[-1] for entry in runs)

    # `guest_run_layout` puts the transport under `<run>/host_tools/`, with the calls beneath
    # that — not directly in the run directory.
    dispatched, left, answered = 0, 0, 0
    for run in directories:
        try:
            entries = await sandbox.list_dir(f"{run}/host_tools", working_directory=spec.work_dir)
        except FileNotFoundError:
            # A run that dispatched nothing. Without a registry the kind uses the flat run
            # directory it always has, so there is no `host_tools/` and nothing was left.
            # Caught by its own type rather than by a bare `except`, because "this run did not
            # dispatch" is the distinction being counted and any other failure should be heard.
            continue
        if not any(entry.path.rstrip("/").endswith(CALLS_DIRECTORY) for entry in entries):
            continue
        dispatched += 1
        files = await sandbox.list_dir(
            f"{run}/host_tools/{CALLS_DIRECTORY}", working_directory=spec.work_dir
        )
        left += len(files)
        answered += sum(1 for entry in files if entry.path.endswith(_RESPONSE_SUFFIX))
    return len(directories), dispatched, left, answered


async def act_five_what_the_runs_left_behind(
    router: SandboxRouter, registry: HostToolRegistry
) -> None:
    """The guest filesystem after both acts, which #302 asks for and #438 half-answers.

    Read with `list_dir`, which needs `Capability.FILES_LIST` — ACAS declares it and Docker does
    not, so this act is one of the reasons the sample belongs on this backend.

    **Both** sandboxes, because there are two: reporting only the dispatched one would leave the
    direct route's runs out of a count the act claims is what the whole sample left behind, and
    would make the contrast invisible — the direct route's sandbox holds run directories with no
    transport files in them at all, which is what "nothing was left" looks like next to the
    other.
    """
    print("== 5. What the runs left in the guest ==\n")
    routes = ((DISPATCH_THREAD, registry), (DIRECT_THREAD, None))
    totals = [await _what_one_sandbox_holds(router, thread, reg) for thread, reg in routes]
    directories = sum(t[0] for t in totals)
    dispatched_runs = sum(t[1] for t in totals)
    left = sum(t[2] for t in totals)
    answered = sum(t[3] for t in totals)

    print(f"{MEASURED}run directories across both sandboxes: {directories}")
    print(f"{MEASURED}of those, runs that dispatched: {dispatched_runs}")
    print(f"{MEASURED}transport files left behind: {left}, of which answered calls: {answered}")
    print()
    print("  A fresh directory per run is what keeps one run's traffic out of the next one's,")
    print("  and on a warm sandbox that is not hypothetical: every run above is still here.")
    print()
    print("  Nothing deleted any of it. The transport has no way to — 'nothing in the protocol")
    print("  deletes' — and no kind is obliged to try (#438). So the count above only ever goes")
    print("  up until the sandbox is disposed of, which is the next line.\n")


async def run() -> int:
    """Wire the stack, run each route against its own sandbox, and take both down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    dispatch_ledger, direct_ledger = Ledger(), Ledger()
    registry = act_one_what_the_host_wired(dispatch_ledger)

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

    credential = DefaultAzureCredential()
    try:
        dispatched = await one_route(
            "2. The lookups happen inside the sandbox",
            DISPATCH_ROUTE,
            env,
            credential,
            codeact_for(router, registry, DISPATCH_THREAD),
            FROM_INSIDE,
            dispatch_ledger,
        )
        direct = await one_route(
            "3. The lookups happen in the model's tool loop",
            DIRECT_ROUTE,
            env,
            credential,
            [*codeact_for(router, None, DIRECT_THREAD), *ordinary(direct_ledger)],
            FROM_THE_TOOL_LIST,
            direct_ledger,
        )
        act_four_what_the_round_trips_bought(dispatched, direct)
        await act_five_what_the_runs_left_behind(router, registry)
    finally:
        # Deletes rather than relying on the lifecycle timers — see sample 01's README. It is
        # also the only thing that removes the files act 5 counted, and the only thing that
        # stops a program a supervisor gave up on (#375).
        deleted = sum(
            [
                await router.dispose_scope(SCOPE, DISPATCH_THREAD),
                await router.dispose_scope(SCOPE, DIRECT_THREAD),
            ]
        )
        print(f"{MEASURED}Disposed {deleted} sandbox(es).")
        # The backend holds pooled clients of its own, so disposing the sandbox is not the
        # whole teardown — samples 01, 03 and 14 close it the same way.
        await backend.aclose()
        await credential.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
