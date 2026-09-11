"""When a sandbox goes away: within a turn, at the end of one, on thread delete — and when it
cannot.

Three disposal moments, and choosing between them is a cost decision rather than a style one.
Every other sample creates a sandbox and drops it on the way out; this one is about what a
long-lived host has to wire, because a sandbox is keyed by the caller's scope, thread and agent directory and
outlives the turn that made it.

Acts 5 to 8 are the fourth thing a host has to decide, and the only one it cannot decide by
reading: a cleanup that **fails**. The removal is made to fail honestly rather than faked — see
`cleanup_probe.py` — and each act takes the host's decision somewhere different. 5: the default,
which disposes the sandbox it could not clean. 6: `FailedReclaimPolicy.KEEP`, the one opt-down.
7: a cleanup budget the engine cannot meet, so the remedy is unproven too and the key is refused.
8: a handler that raises, which changes neither. The host records what it was told through
`maf-sandbox-otel` and a handler of its own, and `docker ps` reports what each decision left.

Needs a Docker-compatible engine. Containers are counted with `docker ps` rather than trusted
from a return value — see this directory's README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox-docker",
#     "maf-sandbox-otel",
#     "maf-sandbox>=0.38",
#     "opentelemetry-sdk",
# ]
# ///

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from _scaffold import MEASURED, installed_versions
from cleanup_probe import PROGRAM_RAN, cleanup_probe_spec, make_cleanup_probe_tools
from maf_sandbox import (
    Cleanup,
    FailedReclaimPolicy,
    Isolation,
    ListedFile,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxUnclean,
)
from maf_sandbox.maf import SandboxPurger, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_otel import CALL, DISPOSE
from telemetry import (
    CALL_ID,
    DISPOSAL,
    DISPOSAL_OUTCOME,
    PATH,
    REASON,
    RECLAIM_FAILURE_SPAN,
    UNCLEAN,
    Telemetry,
    attribute,
    build_telemetry,
    exported,
    for_call,
    outcomes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from maf_sandbox import ReclaimFailure

IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"
SCOPE = "samples"
AGENT_DIR = "assistant"

#: Acts 1 to 4, which never let a cleanup fail.
_THREADS = ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped")
#: Acts 5 to 8, on the hardened backend. One thread each, because each act ends in a different
#: state — a sandbox deleted, one kept on purpose, one whose key is refused — and sharing a
#: thread would make each act's count a reading of the last act's decision.
_LOCKED_THREAD = "t-locked"
_KEPT_UNCLEAN_THREAD = "t-kept-unclean"
_UNPROVEN_THREAD = "t-unproven"
_RAISING_THREAD = "t-raising"
_UNCLEAN_THREADS = (_LOCKED_THREAD, _KEPT_UNCLEAN_THREAD, _UNPROVEN_THREAD, _RAISING_THREAD)

#: Act 7's cleanup budget. Small enough that no engine answers inside it, which is how a
#: disposal is made to fail without anything being faked: the removal is real and the
#: deadline is real, and the host simply never learns which of them won.
_NO_ENGINE_MEETS_IT = 0.001

#: The labels `DockerSandboxBackend` stamps on every container it creates, and the same ones its
#: `dispose_scope` selects on. Short plain values pass through unchanged, which is why the
#: thread ids below are short and plain: it keeps `docker ps` a readable check rather than a
#: digest lookup.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"


def containers(thread_id: str) -> int:
    """How many containers Docker reports for ``thread_id``, **stopped ones included**.

    ``-a`` is the part a caller has to know: a container stopped but not removed still counts,
    which is what makes this answer the same question the backend's own purge listing asks.
    """
    result = subprocess.run(  # noqa: S603 - a fixed argv, no shell, values from this file
        [
            "docker",
            "ps",
            "-a",
            "--quiet",
            "--filter",
            f"label={_LABEL_SCOPE}={SCOPE}",
            "--filter",
            f"label={_LABEL_THREAD}={thread_id}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return len(result.stdout.split())


def spec() -> SandboxSpec:
    """The one spec every act uses. A function so each act states it rather than sharing one."""
    return SandboxSpec(kind="assistant", image=IMAGE)


async def one_turn(router: SandboxRouter, key: SandboxKey) -> None:
    """A turn: acquire, use, return. It does not dispose — that is the host's decision."""
    sandbox = await router.acquire(key, spec())
    await sandbox.write_file("turn", "worked\n", working_directory=spec().work_dir or ".")
    await sandbox.exec("cat turn", working_directory=spec().work_dir or ".", timeout=60)


async def act_one_reuse_within_a_turn(router: SandboxRouter) -> None:
    """What warm reuse actually buys, and the only place it is unambiguously worth having."""
    print("== 1. Within a turn: get-or-create is the point ==\n")

    key = SandboxKey(scope=SCOPE, thread_id="t-reuse", agent_dir=AGENT_DIR)

    # Tested as state surviving rather than with `is`: the protocol promises the same sandbox,
    # not the same object, and the docker backend hands back a fresh handle over one container.
    first = await router.acquire(key, spec())
    await first.write_file(
        "from-first-acquire",
        "still here\n",
        working_directory=spec().work_dir or ".",
    )

    second = await router.acquire(key, spec())
    read_back = await second.exec(
        "cat from-first-acquire", working_directory=spec().work_dir or ".", timeout=60
    )

    print("  wrote a file through the first acquire, read it through the second:")
    print(f"    {read_back.stdout.strip()!r}")
    print(f"  containers for this thread: {containers('t-reuse')}")
    print("  One container, and the second acquire did not pay for a boot. This is what")
    print("  `acquire` being get-or-create is for, and it is not in question below — what is")
    print("  in question is how long the sandbox should outlive the turn.\n")

    await router.dispose_scope(SCOPE, "t-reuse")


async def act_two_between_turns(router: SandboxRouter) -> None:
    """A host that keeps the sandbox between turns, and what that costs where it is billable."""
    print("== 2. Between turns: it survives, and that is a decision ==\n")

    key = SandboxKey(scope=SCOPE, thread_id="t-kept", agent_dir=AGENT_DIR)
    await one_turn(router, key)

    print(f"  turn ended without disposing -> containers still there: {containers('t-kept')}")
    print("  Nothing is wrong with that on Docker: an idle container on your own machine is")
    print("  free, and the next turn starts warm.")
    print()
    print("  On ACAS it has a price, and the two lifecycle bounds are sequential rather than")
    print("  one window. `AcasSandboxConfig` defaults to `auto_suspend_seconds=60` and then")
    print("  `auto_delete_seconds=600`: idle a minute and the sandbox suspends, stopped ten")
    print("  more and it is deleted. A suspended one is resumable, and the backend waits to")
    print("  resume rather than create because a cold create is slower and costs more — so")
    print("  warm reuse really does survive a gap of about eleven minutes.")
    print()
    print("  Which makes the case for purging per turn narrower than it first looks, and")
    print("  still a case. Under eleven minutes the sandbox is there and holding it costs the")
    print("  idle minute before suspension. Hours or days — what a conversation actually")
    print("  looks like — outlives both timers, so that minute is paid every turn and the")
    print("  reuse it bought is gone before the next one arrives. Purging at the end of the")
    print("  turn spends nothing on idling and reclaims when the host decides rather than")
    print("  when the platform does.\n")

    await router.dispose_scope(SCOPE, "t-kept")


async def act_three_purge_at_end_of_turn(router: SandboxRouter) -> None:
    """`router.scope` — the posture a billable backend wants, and one line to adopt."""
    print("== 3. End of turn: `router.scope` disposes however the block ends ==\n")

    thread = "t-perturn"
    async with router.scope(SCOPE, thread) as disposal:
        await one_turn(router, SandboxKey(scope=SCOPE, thread_id=thread, agent_dir=AGENT_DIR))
        print(f"  inside the turn -> containers: {containers(thread)}")

    print(f"  block ended -> router reports {disposal.disposed} disposed")
    print(f"  and docker agrees -> containers: {containers(thread)}")
    print("  The count is read after the block, which is what lets a host log what it")
    print("  reclaimed and notice the day that number is zero. Disposal runs however the")
    print("  block ends, so a turn that raises still reclaims.\n")


async def act_four_thread_delete(router: SandboxRouter) -> tuple[int, int]:
    """`SandboxPurger` on the delete path — a backstop, and the only thing that reclaims a
    conversation whose turns were never scoped.

    Returns what the purger found for each of the two threads, so the footer reports
    measurements rather than the numbers this file expects.
    """
    print("== 4. Thread delete: the backstop ==\n")
    purger = SandboxPurger(router)

    # A conversation whose turns were purged per turn, as act 3 does.
    tidy = "t-tidy"
    async with router.scope(SCOPE, tidy):
        await one_turn(router, SandboxKey(scope=SCOPE, thread_id=tidy, agent_dir=AGENT_DIR))
    tidy_found = (await purger.purge_scoped_thread(SCOPE, tidy)).disposed
    print(f"  a thread already purged per turn -> purger found {tidy_found}")
    print("  Zero is the right answer, not a broken hook. A host that purges at end of turn")
    print("  should expect the delete path to find nothing almost every time.\n")

    # No `router.scope` here on purpose: an entered one disposes however it exits, so never
    # entering it is the only way to reach a delete with work still outstanding.
    unscoped = "t-unscoped"
    await one_turn(router, SandboxKey(scope=SCOPE, thread_id=unscoped, agent_dir=AGENT_DIR))
    print(f"  a thread never scoped per turn -> containers: {containers(unscoped)}")
    unscoped_found = (await purger.purge_scoped_thread(SCOPE, unscoped)).disposed
    print(f"  user deletes the conversation  -> purger found {unscoped_found}")
    print(f"  and docker agrees, after purge -> containers: {containers(unscoped)}")
    print("  Nothing else would have reclaimed this one, and no turn is coming back for it.")
    print("  `dispose_scope` selects on the labels the backend stamped rather than on anything")
    print("  this process remembers, which is what lets the delete path reclaim sandboxes a")
    print("  replica never created — a crashed worker's, or an older deployment's. After it")
    print("  there are only the platform's two timers, reclaiming on their schedule rather")
    print("  than on the host's.\n")

    return tidy_found, unscoped_found


async def _no_files(_store: Any) -> list[ListedFile]:
    """This kind reads no file store, so the caller may act on nothing.

    Required all the same: a `CallerContext` carries three callables and a kind that needs none
    of them still gets one, because which of the three it reads is the kind's business.
    """
    return []


def _hardened_router(
    telemetry: Telemetry,
    policy: FailedReclaimPolicy,
    *,
    on_failure: Callable[[ReclaimFailure], Awaitable[None]] | None = None,
) -> SandboxRouter:
    """A router whose backend drops every capability, and which is told what to do about it.

    `cap_drop_all=True` is the hardening — and the reason a removal in this container can fail
    at all, since root without `CAP_DAC_OVERRIDE` is bound by the same mode bits as anyone.
    `min_cleanup=Cleanup.RECLAIM` is the other half: the per-call directory removal only runs at
    that rung, so a host that never lowers its floor never reaches this failure and disposes
    after every call instead.

    ``on_failure`` defaults to the telemetry handler; act 8 passes one that raises.
    """
    backend = DockerSandboxBackend(DockerSandboxConfig(cap_drop_all=True))
    return SandboxRouter(
        [backend],
        min_isolation=Isolation.CONTAINER,
        min_cleanup=Cleanup.RECLAIM,
        observer=telemetry.observer,
        reclaim=ReclaimConfig(
            # The default, and what bounds everything the router does *outside* a call —
            # including cleaning an instance it has not served before, which act 7 needs to keep
            # working while it starves that call's own cleanup through the per-tool override.
            timeout=30.0,
            failed_reclaim_policy=policy,
            # Set once, on the router, rather than per tool: this is the half a host wires for
            # every kind it attaches, including packaged ones it did not write.
            on_failure=on_failure if on_failure is not None else telemetry.on_reclaim_failure,
        ),
    )


async def _one_locked_call(
    router: SandboxRouter, thread: str, *, reclaim_timeout: float | None = None
) -> str:
    """Attach the probe for one caller and call it once. Returns what the tool answered.

    No model: the tool is invoked directly, because the reclaim and the handler do not care who
    called. `skip_parsing=True` gives the string the body returned rather than the content list
    MAF would hand a model.
    """
    context = make_caller_context(_no_files, lambda: SCOPE, lambda: thread)
    (probe,) = make_cleanup_probe_tools(
        router, AGENT_DIR, context, image=IMAGE, reclaim_timeout=reclaim_timeout
    )
    answer = await probe.invoke(arguments={}, skip_parsing=True)
    return str(answer)


def _report_what_was_recorded(telemetry: Telemetry) -> None:
    """Print the records this call produced, joined the way a pipeline would join them.

    The call id comes from the **package's** own record and the handler's is looked up by it,
    rather than the other way round. That ordering is what makes the reported path worth
    printing: it is the host record's own `app.reclaim.path`, and it matching a call id the
    observer wrote is two independently produced records agreeing.
    """
    call = exported(telemetry.exporter, CALL)[-1]
    call_id = attribute(call, CALL_ID)
    reclaim = for_call(telemetry.exporter, RECLAIM_FAILURE_SPAN, call_id)[-1]
    disposals = for_call(telemetry.exporter, DISPOSE, call_id)

    print(f"  what a collector received for call {call_id}:")
    print(f"    {CALL:<28} {UNCLEAN} = {attribute(call, UNCLEAN)}")
    print(
        f"    {DISPOSE:<28} {len(disposals)} record(s), "
        f"{DISPOSAL_OUTCOME} = {outcomes(disposals, DISPOSAL_OUTCOME)}"
    )
    print(f"    {RECLAIM_FAILURE_SPAN:<28} {DISPOSAL} = {attribute(reclaim, DISPOSAL)}")
    print(f"{MEASURED}Disposal records for this call: {len(disposals)}")
    print(f"{MEASURED}Recorded disposal: {attribute(reclaim, DISPOSAL)}")
    print(f"{MEASURED}Recorded path: {attribute(reclaim, PATH)}")
    print(f"{MEASURED}Recorded reason: {attribute(reclaim, REASON)}")


async def act_five_a_cleanup_that_could_not_run(telemetry: Telemetry) -> None:
    """A reclaim that genuinely fails, the disposal that answers it, and the record of both."""
    print("== 5. When the cleanup cannot run: the framework acts, then tells you ==\n")

    router = _hardened_router(telemetry, FailedReclaimPolicy.DISPOSE)
    spec = cleanup_probe_spec(IMAGE)
    # The precondition the act rests on, measured rather than assumed: at any other rung the
    # call's directory is never removed on its own and there is nothing here to fail.
    print(f"{MEASURED}Cleanup rung for this call: {router.effective_cleanup(spec)}")

    answer = await _one_locked_call(router, _LOCKED_THREAD)
    print(f"  the call answered: {answer!r}")
    print("  The failure is after the answer and does not replace it. A cleanup is not a")
    print("  result, and a host that turned a leak into a failed turn would have made the")
    print("  wrong trade twice.\n")

    print(f"  containers after the escalation: {containers(_LOCKED_THREAD)}")
    print("  Nought, and that is the escalation rather than a tidy ending. The directory")
    print("  could not be emptied, so the sandbox holding it was disposed — `acquire` is")
    print("  get-or-create, so leaving it warm would hand the next call everything this one")
    print("  could not take back. Better a cold start than leaked data.\n")

    _report_what_was_recorded(telemetry)
    print()
    print("  Read them together, because that is the argument. The package's own call record")
    print("  says nought unclean — it counts processes a transport could not prove it stopped,")
    print("  and a directory that would not go is not one of those. Its disposal records say")
    print("  the sandbox went, and there are two of them: the router cleans an instance it has")
    print("  not served before, inside `acquire` and ahead of the body, so the adoption and the")
    print("  escalation are recorded identically and a healthy first call emits the first of")
    print("  them too. So from the package's records alone this call is indistinguishable from")
    print("  one that cleaned up perfectly. The host's record is the only place the path, the")
    print("  reason and what was done about it are written down.\n")


async def act_six_the_one_policy_that_loosens_it(telemetry: Telemetry) -> int:
    """`FailedReclaimPolicy.KEEP`: the same failure, and the sandbox stays with the data in it.

    Returns the container count while the sandbox is still held, so the footer reports what the
    machine said rather than what this file expects.
    """
    print("== 6. `FailedReclaimPolicy.KEEP`: the same failure, kept on purpose ==\n")

    router = _hardened_router(telemetry, FailedReclaimPolicy.KEEP)
    answer = await _one_locked_call(router, _KEPT_UNCLEAN_THREAD)
    print(f"  the call answered: {answer!r}")

    kept = containers(_KEPT_UNCLEAN_THREAD)
    print(f"  containers kept after the failure: {kept}")
    _report_what_was_recorded(telemetry)
    print()
    print("  One container, still there, still holding what the removal could not take. That")
    print("  is the whole of what this setting does, and the record says `kept` rather than")
    print("  `disposed` so a host can tell the two apart in a query rather than by knowing")
    print("  which deployment it was reading.")
    print()
    print("  The one disposal record here is the adoption, from before the body ran. The")
    print("  cleanup that did not happen leaves the package's telemetry with nothing at all —")
    print("  which is the sharper version of act 5's point: not a record reading the same as a")
    print("  healthy call's, but no record.")
    print()
    print("  It is an opt-down, not a tuning knob. The next call on this conversation is")
    print("  served the same sandbox, with the last call's data in it — which is a choice to")
    print("  make deliberately, for a workload where a cold start costs more than the")
    print("  residue. It does not loosen anything else: `min_cleanup` is a separate floor, and")
    print("  a sandbox this router did not clean is still never adopted by a later process.\n")

    await router.dispose_scope(SCOPE, _KEPT_UNCLEAN_THREAD)
    return kept


async def act_seven_a_disposal_nobody_could_prove(telemetry: Telemetry) -> str:
    """The third `disposal` value: the remedy itself did not land, so the key is refused.

    Returns what the next acquire on that key did, so the footer reports the refusal rather
    than this file's expectation of one.
    """
    print("== 7. `disposal='failed'`: the remedy that could not be proved ==\n")

    router = _hardened_router(telemetry, FailedReclaimPolicy.DISPOSE)
    # The per-tool override, and the only place the set shows it. It bounds this call's cleanup
    # and nothing else — which is the whole reason a budget this small is usable here at all.
    # On the router it would starve the cleaning of an unfamiliar instance too, and the call
    # would be refused before its body ran, with no cleanup to fail.
    answer = await _one_locked_call(router, _UNPROVEN_THREAD, reclaim_timeout=_NO_ENGINE_MEETS_IT)
    print(f"  the call answered: {answer!r}")
    _report_what_was_recorded(telemetry)
    print()

    key = SandboxKey(scope=SCOPE, thread_id=_UNPROVEN_THREAD, agent_dir=AGENT_DIR)
    try:
        await router.acquire(key, cleanup_probe_spec(IMAGE))
        refusal = "served"
    except SandboxUnclean:
        refusal = "SandboxUnclean"
    print(f"{MEASURED}The next acquire on that key: {refusal}")
    print(f"  containers left by the unproved disposal: {containers(_UNPROVEN_THREAD)}")
    print()
    print("  Read those two lines together, because they look like a contradiction and are")
    print("  the point. The container is gone: `docker rm` was sent and the daemon finished")
    print("  it. What did not happen is the host **learning** that, inside the budget it set.")
    print("  So `failed` does not mean the sandbox is still there. It means nothing proved it")
    print("  went, and a router that cannot prove a disposal landed refuses the key rather")
    print("  than serving the next call whatever it happens to contain. `KEEP` does not")
    print("  loosen this one: act 6's opt-down is about a reclaim, and this is the remedy.")
    print()
    print("  A one-millisecond budget is not a setting anyone chooses. A budget too small for")
    print("  the engine underneath it is — a loaded daemon, a remote one, a cleanup competing")
    print("  with the next turn — and this is what that host sees.\n")

    await router.dispose_scope(SCOPE, _UNPROVEN_THREAD)
    return refusal


async def act_eight_a_handler_that_raises(telemetry: Telemetry) -> str:
    """A handler that fails does not fail the call, and does not lose what it already recorded.

    Returns the tool's answer, so the footer reports that the raise did not reach the caller.
    """
    print("== 8. A handler that raises is contained ==\n")

    async def records_then_raises(failure: ReclaimFailure) -> None:
        """The recommended order, and the reason for it, in two lines."""
        await telemetry.on_reclaim_failure(failure)
        raise RuntimeError("this host's alerting is down")

    before = len(exported(telemetry.exporter, RECLAIM_FAILURE_SPAN))
    router = _hardened_router(
        telemetry, FailedReclaimPolicy.DISPOSE, on_failure=records_then_raises
    )
    answer = await _one_locked_call(router, _RAISING_THREAD)
    recorded = len(exported(telemetry.exporter, RECLAIM_FAILURE_SPAN)) - before
    # Against the kind's own constant rather than a copy of the sentence, so a body that had
    # stopped running could not satisfy this by leaving the string behind somewhere else.
    reached_the_caller = "unchanged" if answer == PROGRAM_RAN else "replaced"

    print(f"  the call answered: {answer!r}")
    print(f"{MEASURED}The raise reached the caller: {reached_the_caller}")
    print(f"{MEASURED}Records made by the handler that raised: {recorded}")
    print(f"  containers after the raising handler: {containers(_RAISING_THREAD)}")
    print()
    print("  The exception went to the framework's log and no further: the call's answer is")
    print("  the body's, and the sandbox was disposed before the handler ran at all. A host")
    print("  cannot break a cleanup guarantee by writing a bad callback, which is the reason")
    print("  the callback is not where safety is wired.")
    print()
    print("  And the record survived, because the handler wrote it before doing the thing that")
    print("  failed. Reverse those two lines and this act records nothing while every")
    print("  container count above stays correct — a reporting path losing exactly the events")
    print("  it exists for, in the direction that looks healthy. That is not hypothetical: it")
    print("  is the defect review found in sample 07's handler, which printed before it")
    print("  appended, and which nothing could have caught until this act existed.\n")

    await router.dispose_scope(SCOPE, _RAISING_THREAD)
    return reached_the_caller


async def main() -> int:
    """Eight acts against Docker, counted with `docker ps` throughout."""
    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    # One provider for both halves: the package's observer records what the library did, and the
    # handler records what only the host is told. `record_sensitive_data` is the package's own
    # switch and it is on here because everything this sample names is this sample's own — its
    # scope, its thread ids, a container it created. A deployment decides that for itself, and
    # with it off the reason and the path below are withheld while the rest still comes out.
    telemetry = build_telemetry(record_sensitive_data=True)
    try:
        await act_one_reuse_within_a_turn(router)
        await act_two_between_turns(router)
        await act_three_purge_at_end_of_turn(router)
        tidy_found, unscoped_found = await act_four_thread_delete(router)
        await act_five_a_cleanup_that_could_not_run(telemetry)
        kept_unclean = await act_six_the_one_policy_that_loosens_it(telemetry)
        refusal = await act_seven_a_disposal_nobody_could_prove(telemetry)
        contained = await act_eight_a_handler_that_raises(telemetry)
    finally:
        # Whatever any act left behind, however it ended. The sample is about not leaking, so
        # it does not get to leak while saying so — act 6 above all, which ends holding a
        # sandbox on purpose and would otherwise be the one act that leaks.
        for thread in (*_THREADS, *_UNCLEAN_THREADS):
            await router.dispose_scope(SCOPE, thread)

    leftover = sum(containers(thread) for thread in (*_THREADS, *_UNCLEAN_THREADS))
    # Counted off the exporter rather than kept in a variable, so this is what a collector
    # received and not what the handler believes it sent.
    reported = len(exported(telemetry.exporter, RECLAIM_FAILURE_SPAN))
    print(
        f"Completed 8 of 8 acts. Purger found {tidy_found} on a purged thread and "
        f"{unscoped_found} on an unscoped one. Kept after a failed reclaim: {kept_unclean}. "
        f"The key after an unprovable disposal: {refusal}. The answer under a raising "
        f"handler: {contained}. Reclaim failures recorded: {reported}. Containers left "
        f"behind: {leftover}."
    )
    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(main()))
