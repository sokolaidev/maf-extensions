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
batches *within* a stage but never across one, so it pays a tool-calling round per stage
and every value it fetched crosses the conversation.  Dispatch pays a transport round trip
per call — serially, always (#439) — and the model writes none of the data into code:
what comes back to it is the program's finished table.

Act 5 is what the runs left in the guest, which #302 asks for and which is only half
answerable, and the half that is moved: a fresh directory per run is real, and whether the
traffic in it survives the run depends on which transport is installed — #434 gave the
transport a cleanup for its own files, where before nothing in the protocol could delete
anything (#438). Act 5 asks which it is running against and grades that one.

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
import os
import re
import sys
import time
from collections.abc import Iterable
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
    EntryKind,
    HostToolRegistry,
    Identity,
    SandboxKey,
    SandboxRouter,
    SourceIntegrity,
    sandbox_tool,
)
from maf_sandbox.maf import list_all_files, make_caller_context

#: Whether the installed transport takes its own files back when a run ends. #434 gave it a
#: cleanup and exported `reclaim_run` in the same release, so the import is the marker for
#: both: before it, `dispatch_over_exec` left the request and response files in the guest and
#: nothing in the protocol could remove them (#438); after it, the transport removes the
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

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
AGENT_DIR = "analyst"

#: A sandbox each, and the reason is act 5. Nothing deletes a run's transport files, so the
#: dispatched route's responses — every state id, store list, sales row and product name —
#: stay readable on the guest filesystem for anything that runs there afterwards. Sharing one
#: sandbox would give the direct route's program a second road to the same data that this
#: sample never measures, and the comparison rests on there being only one.
#:
#: Two keys rather than a cleanup between them, and it stays two keys after #434. That gave
#: the transport a cleanup for the files it owns, which closes this leak on the cores that have
#: it — but a sample runs against whatever is published, and one sandbox would be right on the
#: new transport and wrong on the old one. Two is right on both.
#: And a run in the id, not only a route. `dispose_scope` selects on the labels the backend
#: stamped and asks the *service* for them rather than this process, so an id two runs share
#: is a delete they share — and the runs that verify a release are concurrent against one
#: sandbox group. Unique per run, then, and short enough to stay a readable label rather than
#: the digest the backend substitutes past its length limit. #445 is the other samples.
#:
#: The cost, since it is a trade rather than a free fix: an id nothing reuses is an id nothing
#: tidies. A run killed before act 6 leaves a sandbox for the group's lifecycle timers, which
#: is the #375 exposure the footer count exists to make visible.
_RUN = os.environ.get("GITHUB_RUN_ID") or f"local-{os.getpid()}"
_ATTEMPT = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
DISPATCH_THREAD = f"15-host-tools-{_RUN}-{_ATTEMPT}-dispatch"
DIRECT_THREAD = f"15-host-tools-{_RUN}-{_ATTEMPT}-direct"

#: Sample 14's image, available to the sandbox group as a disk image. The transport's launcher
#: is POSIX shell and its shim is Python, so the guest needs `sh`, `nohup`, `mkdir`, `mv`,
#: `printf` and `python3` — a distroless or Windows image cannot serve this whatever it
#: declares.
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
    def stages(self) -> set[str]:
        """Which of the four lookups were actually reached.

        A count alone does not say the walk happened: the per-state totals are sums of the
        sales amounts, so a program can skip `product_name` entirely, print both totals and
        look complete while never producing the by-product table the task asks for — and never
        touching the fourth stage the comparison is about.
        """
        return {asked.split("(", 1)[0] for asked in self.asked}

    def round_trips(self, programs: int) -> tuple[list[float], list[float]]:
        """The gaps between consecutive calls, split into transport ones and program boundaries.

        One entry per consecutive pair, so *n* calls yield *n - 1*. A route runs `programs`
        separate programs against the same ledger, and the gap between the last call of one and
        the first call of the next holds a model turn and a launcher rather than a file round
        trip. There are exactly `programs - 1` of those while the programs run one after
        another, which is what the live check enforces: calls in one assistant message run
        concurrently, and two programs interleaved in one ledger have no boundary to find.

        **Which gaps they are is inferred from size, not recorded.** Nothing here is told what
        program a call belongs to, so this takes the largest `programs - 1` and calls them the
        boundaries. Both halves are returned rather than one, because that inference is only
        sound while the two are far apart — a model turn is seconds where a file written and
        read back is about one — and the run publishes the margin so the check can hold them
        apart instead of trusting the sort. Attributing a call to its program would take the
        transport saying which run it served, which is a library surface this sample does not
        have (#446).
        """
        gaps = sorted(
            self._arrived[index + 1] - self._answered[index]
            for index in range(len(self._answered) - 1)
        )
        kept = len(gaps) - max(0, programs - 1)
        return gaps[:kept], gaps[kept:]


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

    Tool-call *arguments* only, not the whole transcript, and the narrowness is the point: what
    it answers is whether the model had to carry a value from one place to another. On the
    direct route that is forced — the figures arrive as tool results and the only way into the
    program is for the model to write them into its source. On the dispatched route it is
    impossible, because the program is written before any dispatch can answer.

    A dispatched program may still *print* a figure, and then it is in the transcript by that
    program's choice rather than by the transport's design. Different claim, different line.

    By value, for the reason `figures_in` is: `980.00` written as `980` is the same figure
    carried, and `1980.0` is not that figure at all. Both directions matter here, because both
    routes assert against this count — the dispatched one that it is zero.
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

    Numerically rather than as text: a program summing floats prints `1791.1499999999999` for a
    cell worth `1791.15`, and how it formats its output is the model's to choose.
    """
    printed = [_as_number(found) for found in _PRINTED_NUMBER.findall(text)]
    return sum(1 for want in expected if any(abs(got - want) < 0.005 for got in printed))


def rows_in(text: str) -> int:
    """How many of the six cells the program printed *as rows*, state and product attached.

    The values alone are a multiset: swapping the two states' figures leaves the same six
    numbers and the same two totals. A row is matched on its product name and amount together,
    with the state read off the line itself or, for a table that groups by state, off the last
    state named above it — and on naming *one* product, without which a line carrying all three
    of a state's pairs answers for three rows at once.
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

    One function because it is two things that must not disagree — the figures a route reports
    and the key that picks the program they are reported for.

    Ordered by what is asked of *both* routes rather than by how specific it is. Every route
    has to print the six cells and the two state totals; only the dispatched one has to label
    them, because only there does a label prove the walk happened. Lead on the labels and a
    direct-route program that printed the whole answer unlabelled loses to one that labelled a
    single row, which is a complete answer thrown away. Cells and totals cannot separate a
    table from the same six values against the wrong states, so the rows break that tie
    underneath them, which is the job they were added for.
    """
    return (
        figures_in(printed, PRODUCT_CELLS),
        figures_in(printed, STATE_TOTALS.values()),
        rows_in(printed),
        sum(1 for product in PRODUCTS.values() if product in printed),
    )


def the_program_that_answered(results: list[str]) -> str:
    """The one `execute_code` result the table was in, of however many the route ran.

    Joining them would let two programs satisfy the checks between them — one printing
    Washington, the other Oregon — where the task asks for one table and this route's
    instruction asks for one program that owns the whole walk. Ranked on what the route is
    graded on, so the order the programs ran in cannot decide which one answers for the route.
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
    if route == DISPATCH_ROUTE:
        # Programs, not rounds. `calls_per_message` groups by message and one message can ask
        # for several calls at once, so the number of programs is the sum of the groups rather
        # than how many groups there are — on this route every call is an `execute_code`, the
        # lookups being host tools the model never sees. Passing the round count would leave
        # one boundary per batched message in the sample, timing a model turn as a round trip.
        trips, boundaries = ledger.round_trips(sum(calls_per_message(response)))
        if trips:
            print(
                f"{MEASURED}{route}: round trip: {len(trips)} gap(s), min {min(trips):.2f}s, "
                f"median {median(trips):.2f}s, max {max(trips):.2f}s"
            )
        if trips and boundaries:
            # What the line above rests on. The boundaries were chosen by being the largest,
            # so the smallest of them exceeding the largest kept gap is arithmetic; how far it
            # exceeds it is the measurement, and it is what says the two are different things.
            print(
                f"{MEASURED}{route}: program boundaries dropped: {len(boundaries)}, smallest "
                f"{min(boundaries):.2f}s against a {max(trips):.2f}s largest round trip"
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

    Acquiring returns the *same warm sandbox* the route used — same key, same spec — which is
    the point of the act: the runs are still in it.
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
    """The guest filesystem after both acts, which #302 asks for and the answer to which moved.

    Read with `list_dir`, which needs `Capability.FILES_LIST` — ACAS declares it and Docker does
    not, so this act is one of the reasons the sample belongs on this backend.

    **Both** sandboxes, because there are two: reporting only the dispatched one would leave the
    direct route's runs out of a count the act claims is what the whole sample left behind.

    What the counts mean depends on the transport, so the transport is named first. Where it
    keeps its files, they are the run's traffic and the guest can be asked how many programs
    dispatched. Where it reclaims them, zero is the cleanup working and the question has no
    answer here — the run directories still say programs ran, and nothing in the guest says
    which of them called out. That is a real loss of corroboration and the check says so in
    the one place it now takes the model's word for the program count.
    """
    print("== 5. What the runs left in the guest ==\n")
    routes = ((DISPATCH_THREAD, registry), (DIRECT_THREAD, None))
    totals = [await _what_one_sandbox_holds(router, thread, reg) for thread, reg in routes]
    directories = sum(t[0] for t in totals)
    dispatched_runs = sum(t[1] for t in totals)
    left = sum(t[2] for t in totals)
    answered = sum(t[3] for t in totals)

    print(f"{MEASURED}transport cleanup: {RECLAIMED if TRANSPORT_RECLAIMS else KEPT}")
    print(f"{MEASURED}run directories across both sandboxes: {directories}")
    print(f"{MEASURED}of those, runs that dispatched: {dispatched_runs}")
    print(f"{MEASURED}transport files left behind: {left}, of which answered calls: {answered}")
    print()
    print("  A fresh directory per run is what keeps one run's traffic out of the next one's,")
    print("  and on a warm sandbox that is not hypothetical: every run above is still here.")
    print()
    if TRANSPORT_RECLAIMS:
        print("  The traffic is not. `dispatch_over_exec` removes the directory it owns on every")
        print("  exit path, so the requests and responses above are gone and zero is the cleanup")
        print("  having worked (#434). What it does not remove is the run itself or the model's")
        print("  files — those are the kind's, through `reclaim_run` — which is why the")
        print("  directories are still counted and only their contents are not.")
        print()
        print("  The cost lands here: the guest no longer corroborates how many programs called")
        print("  out, so that count comes from the host's own record alone.\n")
    else:
        print("  Nothing deleted any of it. This transport has no way to — 'nothing in the")
        print("  protocol deletes' — and no kind is obliged to try (#438). So the count above")
        print("  only ever goes up until the sandbox is disposed of, which is the next line.\n")


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
