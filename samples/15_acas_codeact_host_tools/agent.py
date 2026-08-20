"""A program inside a sandbox calling back into the host, and what the round trips buy.

Every other sample sends things *in* and takes results *out*.  This one opens the other
direction::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_codeact calls the router        |
                  +------ a host function, dispatched ----------+

Sample 10 is the configuration half and this is the traffic half (#302); read 10 first, since
the acts below use its vocabulary.  #133 asks for the trade-off to be measured rather than
assumed, so the workload is deliberately call-heavy and deep: three tables and a question that
cannot be answered without walking them in order.

Acts 2 and 3 answer it twice and **both run Python in the sandbox** — only the place of the
lookups differs, which is what keeps this a measurement of dispatch rather than of CodeAct.
Act 4 compares them, act 5 reads the guest filesystem, act 6 takes the sandboxes down.

The README has the prerequisites, the environment variables and the reasoning behind each act.

Running this needs a real Azure subscription and **creates two billable sandboxes**, one per
route, which the README explains and act 6 disposes of.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-acas>=0.9",
#     "maf-sandbox-codeact>=0.5",
#     "maf-sandbox>=0.18",
# ]
# ///

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from collections.abc import Iterable
from contextvars import ContextVar
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _scaffold import MEASURED, conversation_id, quoted, require_env_vars, tool_results
from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    CALLS_DIRECTORY,
    EntryKind,
    HostToolRegistry,
    HostToolRun,
    Identity,
    SandboxKey,
    SandboxRouter,
    SourceIntegrity,
    sandbox_tool,
)
from maf_sandbox.maf import list_all_files, make_caller_context

#: Whether the installed transport takes its own files back when a run ends. #434 gave it a
#: cleanup and exported `reclaim_run` in the same release, so the import is the marker for
#: transport cleanup: before it, `dispatch_over_exec` left the request and response files in the
#: guest; after it, the transport removes the directory it owns. The framework's call-directory
#: cleanup is reported separately because it belongs to the CodeAct kind's installed version.
#: directory it owns on every exit path. A sample runs against whatever is on PyPI, and act 5
#: asks rather than assumes — the two are different measurements and both are correct.
try:
    from maf_sandbox import reclaim_run as _reclaim_run
except ImportError:  # the published core before #434
    _reclaim_run = None

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import codeact_sandbox_spec, make_codeact_tools

TRANSPORT_RECLAIMS = _reclaim_run is not None
RECLAIMED, KEPT = "reclaimed by the transport", "left for the sandbox (#438)"
CALL_RECLAIMED, CALL_KEPT = "reclaimed by the framework", "left for the sandbox (#438)"

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
AGENT_DIR = "analyst"

#: A sandbox each, and the reason is act 5. On a legacy transport, the dispatched route's
#: responses remain readable on the guest filesystem for anything that runs there afterwards.
#: Sharing one sandbox would give the direct route's program a second road to the same data that
#: this sample never measures. Newer transports reclaim their own files, but the sample runs
#: against whatever is published, so two sandboxes remain correct for both transport behaviors.
#: And a run in each, not only a route: `conversation_id` says why, and #445 is the rest of
#: the samples that needed it. The one thing that is this sample's own is that there are two —
#: a sandbox apiece, so neither route can read the other's leftovers.
DISPATCH_THREAD = conversation_id("15-host-tools-dispatch")
DIRECT_THREAD = conversation_id("15-host-tools-direct")

#: Sample 14's image, available to the sandbox group as a disk image. The transport's launcher
#: is POSIX shell and its shim is Python, so the guest needs `sh`, `nohup`, `mkdir`, `mv`,
#: `printf`, `rm`, `kill` and `python3`, and uses `setsid` when available. A distroless
#: or Windows image cannot serve this whatever it declares.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: Three tables in the host process, and the spec below asks for no `egress_allow`, so the
#: guest initiates nothing: a program that wants any of this has one road to it.
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
#: leaving them implicit, because the count is the measurement: a stage costs direct tool
#: calling one tool-calling round, and dispatch none.
STAGES = ("state_id", "stores_in_state", "store_sales", "product_name")

#: A served call leaves three files: the id its caller claimed, the request, and the answer.
#: Counting the answers separately is what makes the total legible — a bare file count reads
#: as three times the traffic there was.
_RESPONSE_SUFFIX = ".response.json"

#: The two roads, spelled once.
DISPATCH_ROUTE = "dispatch route"
DIRECT_ROUTE = "direct route"

#: Every sales figure, as a number. Act 4 reports how many the model had to write down
#: against how many there are, so a run that carried some but not all is visible rather than
#: rounded to "some".
AMOUNTS = {amount for rows in SALES.values() for _, amount in rows}


def truth() -> dict[str, dict[str, float]]:
    """The answer, computed host-side rather than by either route, so it can check them."""
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

#: Every per-state, per-product cell of the answer, which the two state totals do not
#: establish: a total is a sum, and a sum survives losing a row underneath it.
PRODUCT_CELLS = tuple(amount for rows in TRUTH.values() for amount in rows.values())

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
#: costs a tool-calling round per step.
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


_current_run: ContextVar[HostToolRun | None] = ContextVar("sample_15_host_tool_run", default=None)


@contextlib.contextmanager
def observe_dispatch(run: HostToolRun, _name: object):
    """Attribute the observed dispatch body to its CodeAct run."""
    token = _current_run.set(run)
    try:
        yield
    finally:
        _current_run.reset(token)


class Ledger:
    """What the host was asked, when, and by which guest program."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._arrived: list[float] = []
        self._answered: list[float] = []
        self._runs: list[HostToolRun | None] = []
        self.dispatched_runs: set[HostToolRun] = set()

    def arriving(self, what: str) -> None:
        self.asked.append(what)
        self._arrived.append(time.perf_counter())
        self._runs.append(_current_run.get())
        run = _current_run.get()
        if run is not None:
            self.dispatched_runs.add(run)

    def answered(self) -> None:
        self._answered.append(time.perf_counter())

    @property
    def stages(self) -> set[str]:
        """Which of the four lookups were reached.

        A count is not enough: the totals are sums, so a program can skip `product_name`, print both
        and look complete while never touching the stage the comparison is about.
        """
        return {asked.split("(", 1)[0] for asked in self.asked}

    def round_trips(self) -> tuple[list[float], list[float]]:
        """Split call gaps by the run identity recorded by the host observer.

        A gap within one run is a transport round trip; a gap across runs is a program boundary.
        """
        gaps: list[float] = []
        boundaries: list[float] = []
        for index in range(len(self._answered) - 1):
            gap = self._arrived[index + 1] - self._answered[index]
            if self._runs[index] is self._runs[index + 1]:
                gaps.append(gap)
            else:
                boundaries.append(gap)
        return gaps, boundaries


def build(stamp: Any, ledger: Ledger) -> list[Any]:
    """The four lookups, stamped for dispatch or wrapped as ordinary MAF tools.

    One body per lookup, so a difference between the acts is never a difference in the function.
    `register` keys on `__name__`, so these keep the names the guest calls. Sample 10's README
    covers the declarations.
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
    """No `@sandbox_tool` stamp, which is act 3's subject rather than an oversight.

    A tool the model calls itself crosses the conversation, not a sandbox boundary.
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

    Every entry is one tool-calling round: the turn stops, the framework runs what was asked, the
    model is invoked again. A message with no tool call is not an entry, so each route is invoked
    once more than this counts — both equally, which is why the difference between them holds.
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


#: What a separator is worth: nothing. Both spellings of one are dropped before the parse,
#: which is also why one function serves two patterns that allow different ones.
def _as_number(found: str) -> float:
    return float(found.replace(",", "").replace("_", ""))


#: A number as a tool call's arguments carry one. The envelope is JSON but `code` holds Python,
#: so the underscore separator is in scope and the comma is not: `980`, `980.0`, `980.00`,
#: `9.8e2` and `1_240.50` are all figures written down as a model writes them. No decimal point
#: is required either. The two guards keep the digits of an identifier — `PRD-1`, `STO-202` —
#: and of anything a number is only the front of from reading as a value the model chose.
_WRITTEN_NUMBER = re.compile(r"(?<![\w.])-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?(?!\w)")


def amounts_the_model_wrote(response: object) -> int:
    """How many distinct sales figures the model itself put into a tool call.

    Arguments only: the question is whether the model had to carry a value from one place to
    another. Forced on the direct route, impossible on the dispatched one. Matched by value, so
    `980` is `980.00` written down and `1980.0` is neither.
    """
    written: set[float] = set()
    for message in getattr(response, "messages", []):
        for content in getattr(message, "contents", []) or []:
            body = str(getattr(content, "arguments", "") or "")
            numbers = [_as_number(found) for found in _WRITTEN_NUMBER.findall(body)]
            written |= {want for want in AMOUNTS if any(abs(got - want) < 0.005 for got in numbers)}
    return len(written)


#: A decimal number as a program prints one: grouping, an exponent, and the sign, without
#: which a table of correct magnitudes all negated reads as the right answer. How the output is
#: formatted is the model's to choose, and Python offers it two groupings — `f"{1791.15:,}"` is
#: `1,791.15` and `f"{1791.15:_}"` is `1_791.15` — either of which is that cell. The lookbehind
#: keeps a hyphen that belongs to a label — `WA-1896.25` — from making one negative. The decimal
#: point stays required, unlike the one a tool call carries: it is what keeps a run id like
#: `1e3f4a…` from reading as a thousand now that an exponent is allowed.
_PRINTED_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,_]*\.\d+(?:[eE][+-]?\d+)?")

#: What the CodeAct kind names a run directory: `uuid4().hex[:12]`.
_RUN_ID = re.compile(r"[0-9a-f]{12}")


def figures_in(text: str, expected: Iterable[float]) -> int:
    """How many of `expected` the guest program printed, matched to the cent.

    By value, not as text: a program summing floats prints `1791.1499999999999` for `1791.15`.
    """
    printed = [_as_number(found) for found in _PRINTED_NUMBER.findall(text)]
    return sum(1 for want in expected if any(abs(got - want) < 0.005 for got in printed))


def rows_in(text: str) -> int:
    """How many of the six cells the program printed *as rows*, state and product attached.

    The values alone are a multiset — swapping the two states leaves them and both totals intact.
    A row names one product and carries its amount, with the state on the line or above it.
    """
    current, state_of = None, []
    lines = text.splitlines()
    for line in lines:
        named = [state for state in STATES if state in line]
        current = named[0] if len(named) == 1 else current
        state_of.append(current)
    named = [[name for name in PRODUCTS.values() if name in line] for line in lines]
    return sum(
        any(
            state_of[index] == state and named[index] == [product] and figures_in(line, [amount])
            for index, line in enumerate(lines)
        )
        for state, products in TRUTH.items()
        for product, amount in products.items()
    )


def graded(printed: str) -> tuple[int, int, int, int]:
    """What one program's output is worth: cells, totals, rows, names, in that order.

    One function, because the figures a route reports and the key that picks the program they are
    reported for must not disagree. Ordered by what both routes owe: only the dispatched one has
    to label its table, so leading on labels throws away an unlabelled direct-route answer.
    """
    return (
        figures_in(printed, PRODUCT_CELLS),
        figures_in(printed, STATE_TOTALS.values()),
        rows_in(printed),
        sum(1 for product in PRODUCTS.values() if product in printed),
    )


def the_program_that_answered(results: list[str]) -> str:
    """The one `execute_code` result the table was in, of however many the route ran.

    Not joined, because the task asks for one program that owns the whole walk. Ranked on what
    the route is graded on, so the order the programs ran in cannot decide it.
    """
    return max(results, key=graded, default="")


def report(
    route: str, seconds: float, usage: dict[str, Any], ledger: Ledger, response: object
) -> int:
    """One route's numbers, on the tagged lines the live check reads."""
    grouped = calls_per_message(response)
    printed = the_program_that_answered(tool_results(response, "execute_code"))
    cells, totals, rows, named = graded(printed)
    print(
        f"{MEASURED}{route}: {len(ledger.asked)} lookup(s) over {len(grouped)} "
        f"tool-calling round(s)"
    )
    print(f"{MEASURED}{route}: tool calls per round: {grouped}")
    print(
        f"{MEASURED}{route}: lookup stages exercised: {len(ledger.stages)} of {len(STAGES)} "
        f"({', '.join(sorted(ledger.stages))})"
    )
    print(
        f"{MEASURED}{route}: {seconds:.2f}s, {usage.get('total_token_count')} tokens "
        f"(in {usage.get('input_token_count')}, cached {usage.get('cache_read_input_token_count')}, "
        f"out {usage.get('output_token_count')})"
    )
    print(f"{MEASURED}{route}: state totals the program printed: {totals} of {len(STATE_TOTALS)}")
    # The totals alone would pass a program that printed them and nothing underneath, so the
    # cells are counted separately: this is the table the task asked for, row by row.
    print(f"{MEASURED}{route}: product totals the program printed: {cells} of {len(PRODUCT_CELLS)}")
    print(f"{MEASURED}{route}: product names in the table: {named} of {len(PRODUCTS)}")
    # Required of the dispatched route only, for the reason the names are: that model is never
    # handed a product name, so a row it labelled correctly can only have come from the walk.
    print(f"{MEASURED}{route}: table rows the program printed: {rows} of {len(PRODUCT_CELLS)}")
    carried = amounts_the_model_wrote(response)
    print(
        f"{MEASURED}{route}: sales figures the model wrote into code: {carried} of {len(AMOUNTS)}"
    )
    return carried


def act_one_what_the_host_wired(ledger: Ledger) -> HostToolRegistry:
    """Registration and the seal, in the shortest form that is still honest."""
    print("== 1. What the host wired ==\n")
    registry = HostToolRegistry(
        require_declared=True,
        max_dispatches_per_run=DISPATCH_CAP,
        dispatch_observer=observe_dispatch,
    )
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
    if route == DISPATCH_ROUTE:
        trips, boundaries = ledger.round_trips()
        print(f"{MEASURED}{route}: programs that dispatched: {len(ledger.dispatched_runs)}")
        if trips:
            print(
                f"{MEASURED}{route}: round trip: {len(trips)} gap(s), min {min(trips):.2f}s, "
                f"median {median(trips):.2f}s, max {max(trips):.2f}s"
            )
        if boundaries:
            print(
                f"{MEASURED}{route}: program boundaries observed: {len(boundaries)}, min "
                f"{min(boundaries):.2f}s, max {max(boundaries):.2f}s"
            )
    print()
    return carried


def act_four_what_the_round_trips_bought(dispatched: int, direct: int) -> None:
    """The comparison, once correctness has stopped being the variable."""
    print("== 4. What the round trips bought ==\n")
    total = len(AMOUNTS)
    # "wrote into code", not "handled": the dispatched program's printed table goes back to
    # the model as a tool result, and it carries figures. What never happens on that route is
    # the model putting one into a call of its own, which is the narrower thing measured here.
    print(f"{MEASURED}sales figures the model wrote into code, dispatched: {dispatched} of {total}")
    print(f"{MEASURED}sales figures the model wrote into code, direct:     {direct} of {total}")
    print()
    print("  Both routes ran Python and both reached the same table, so correctness is not what")
    print("  a round trip buys — sample 03 already showed what an interpreter is for.")
    print()
    print("  Direct tool calling batches within a stage and never across one, so it pays one")
    print("  tool-calling round per stage of the walk, and every figure it fetched had to be")
    print("  written back into the program by the model. Those values are in the transcript,")
    print("  the context window, and whatever logs either of them reaches — and they stay")
    print("  there, turn after turn, which is a ceiling long before it is a bill.")
    print()
    print("  Dispatch pays a transport round trip per call instead, serially and with no batching")
    print("  available at any layer (#439), and the model writes none of the data into code —")
    print("  what comes back to it is the program's finished table. That is the trade:")
    print("  wall clock, which is spent per run, against context, which accumulates.\n")


async def _what_one_sandbox_holds(
    router: SandboxRouter, thread: str, registry: HostToolRegistry | None
) -> tuple[int, int, int, int]:
    """Run directories, how many dispatched, and the files those left, for one route's sandbox.

    Acquiring returns the same warm sandbox the route used, which is the point: the runs are in it.
    """
    spec = codeact_sandbox_spec(image=CODEACT_IMAGE, host_tools=registry)
    sandbox = await router.acquire(SandboxKey(SCOPE, thread, AGENT_DIR), spec)
    runs = await sandbox.list_dir(".", working_directory=spec.work_dir)
    # Kind *and* name, because the guest can write here: a program that walks up out of its
    # work directory can leave a file beside the runs, and `list_dir` on `<file>/host_tools`
    # raises rather than reporting nothing. The kind names a run `uuid4().hex[:12]`.
    named = (
        entry.path.rstrip("/").split("/")[-1] for entry in runs if entry.kind is EntryKind.DIRECTORY
    )
    directories = sorted(name for name in named if _RUN_ID.fullmatch(name))

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
    """The guest filesystem after both acts, which #302 asks for.

    Both sandboxes, and `list_dir` needs `Capability.FILES_LIST` — ACAS declares it, Docker does
    not, which is one reason this sample belongs on this backend.

    What the counts mean depends on the transport, so it is named first. Where it keeps its files
    they are the run's traffic; where it reclaims them, zero is the cleanup working and the guest
    no longer corroborates how many programs called out.
    """
    print("== 5. What the runs left in the guest ==\n")
    routes = ((DISPATCH_THREAD, registry), (DIRECT_THREAD, None))
    totals = [await _what_one_sandbox_holds(router, thread, reg) for thread, reg in routes]
    directories = sum(t[0] for t in totals)
    dispatched_runs = sum(t[1] for t in totals)
    left = sum(t[2] for t in totals)
    answered = sum(t[3] for t in totals)

    print(f"{MEASURED}transport cleanup: {RECLAIMED if TRANSPORT_RECLAIMS else KEPT}")
    call_reclaims = directories == 0
    print(f"{MEASURED}call directory cleanup: {CALL_RECLAIMED if call_reclaims else CALL_KEPT}")
    print(f"{MEASURED}run directories across both sandboxes: {directories}")
    print(f"{MEASURED}of those, runs that dispatched: {dispatched_runs}")
    print(f"{MEASURED}transport files left behind: {left}, of which answered calls: {answered}")
    print()
    print("  A fresh directory per run keeps one run's traffic out of the next one's.")
    if call_reclaims:
        print("  The framework-owned call directories are gone when act 5 looks.")
    else:
        print("  On older CodeAct kinds, those call directories remain for act 5 to count.")
    print()
    if TRANSPORT_RECLAIMS:
        print("  The traffic is not. `dispatch_over_exec` removes the directory it owns on every")
        print("  exit path, so the requests and responses above are gone and zero is the cleanup")
        print("  having worked (#434). The framework may also reclaim the whole call directory")
        print("  after the kind returns; when it does, the host's observer is the only run count.")
        print()
        print("  The cost lands here: the guest no longer corroborates how many programs called")
        print("  out, so that count comes from the host's own record alone.\n")
    else:
        print("  The transport leaves its traffic behind on this older release, and the kind")
        print("  leaves its call directories behind too. The count remains useful until the")
        print("  sandbox is disposed of, which is the next line.\n")


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
