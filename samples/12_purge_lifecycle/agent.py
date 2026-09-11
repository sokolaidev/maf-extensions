"""When a sandbox goes away: within a turn, at the end of one, on thread delete — and when it
cannot.

Three disposal moments, and choosing between them is a cost decision rather than a style one.
Every other sample creates a sandbox and drops it on the way out; this one is about what a
long-lived host has to wire, because a sandbox is keyed by the caller's scope, thread and agent directory and
outlives the turn that made it.

Acts 5 and 6 are the fourth thing a host has to decide, and the only one it cannot decide by
reading: a cleanup that **fails**. The removal is made to fail honestly rather than faked — see
`cleanup_probe.py` — the framework disposes the sandbox it could not clean, and the host records
what it was told through `maf-sandbox-otel` and a handler of its own. Act 6 runs the same call
under the one policy that loosens that, and `docker ps` reports the difference.

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
from typing import Any

from _scaffold import MEASURED, installed_versions
from cleanup_probe import cleanup_probe_spec, make_cleanup_probe_tools
from maf_sandbox import (
    Cleanup,
    FailedReclaimPolicy,
    Isolation,
    ListedFile,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.maf import SandboxPurger, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_otel import CALL, DISPOSE
from telemetry import (
    CALL_ID,
    DISPOSAL,
    DISPOSAL_OUTCOME,
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

IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"
SCOPE = "samples"
AGENT_DIR = "assistant"

#: Acts 1 to 4, which never let a cleanup fail.
_THREADS = ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped")
#: Acts 5 and 6, on the hardened backend. One each, because the second act's whole point is a
#: container the first act's policy would have deleted.
_LOCKED_THREAD = "t-locked"
_KEPT_UNCLEAN_THREAD = "t-kept-unclean"

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


def _hardened_router(telemetry: Telemetry, policy: FailedReclaimPolicy) -> SandboxRouter:
    """A router whose backend drops every capability, and which is told what to do about it.

    `cap_drop_all=True` is the hardening — and the reason a removal in this container can fail
    at all, since root without `CAP_DAC_OVERRIDE` is bound by the same mode bits as anyone.
    `min_cleanup=Cleanup.RECLAIM` is the other half: the per-call directory removal only runs at
    that rung, so a host that never lowers its floor never reaches this failure and disposes
    after every call instead.
    """
    backend = DockerSandboxBackend(DockerSandboxConfig(cap_drop_all=True))
    return SandboxRouter(
        [backend],
        min_isolation=Isolation.CONTAINER,
        min_cleanup=Cleanup.RECLAIM,
        observer=telemetry.observer,
        reclaim=ReclaimConfig(
            # The default, stated because it bounds two things a host may not expect it to: the
            # removal itself — a `docker exec` here, which can hang where the engine does — and
            # the handler below, which runs inside the same budget.
            timeout=30.0,
            failed_reclaim_policy=policy,
            # Set once, on the router, rather than per tool. `sandboxed_tool` takes an
            # `on_reclaim_failure=` override, and a host that wants one policy for every kind it
            # attaches — including packaged ones it did not write — sets it here.
            on_failure=telemetry.on_reclaim_failure,
        ),
    )


async def _one_locked_call(router: SandboxRouter, thread: str) -> str:
    """Attach the probe for one caller and call it once. Returns what the tool answered.

    No model: the tool is invoked directly, because the reclaim and the handler do not care who
    called. `skip_parsing=True` gives the string the body returned rather than the content list
    MAF would hand a model.
    """
    context = make_caller_context(_no_files, lambda: SCOPE, lambda: thread)
    (probe,) = make_cleanup_probe_tools(router, AGENT_DIR, context, image=IMAGE)
    answer = await probe.invoke(arguments={}, skip_parsing=True)
    return str(answer)


def _report_what_was_recorded(telemetry: Telemetry) -> None:
    """Print the three records this call produced, selected by its own call id.

    The handler's record is what names the call, so it is read first and the package's two are
    found from it. That order is also the join a pipeline makes, run here against one exporter
    instead of a query.
    """
    reclaim = exported(telemetry.exporter, RECLAIM_FAILURE_SPAN)[-1]
    call_id = attribute(reclaim, CALL_ID)
    (call,) = for_call(telemetry.exporter, CALL, call_id)
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


async def main() -> int:
    """Six acts against Docker, counted with `docker ps` throughout."""
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
    finally:
        # Whatever any act left behind, however it ended. The sample is about not leaking, so
        # it does not get to leak while saying so — act 6 above all, which ends holding a
        # sandbox on purpose and would otherwise be the one act that leaks.
        for thread in (*_THREADS, _LOCKED_THREAD, _KEPT_UNCLEAN_THREAD):
            await router.dispose_scope(SCOPE, thread)

    leftover = sum(
        containers(thread) for thread in (*_THREADS, _LOCKED_THREAD, _KEPT_UNCLEAN_THREAD)
    )
    # Counted off the exporter rather than kept in a variable, so this is what a collector
    # received and not what the handler believes it sent.
    reported = len(exported(telemetry.exporter, RECLAIM_FAILURE_SPAN))
    print(
        f"Completed 6 of 6 acts. Purger found {tidy_found} on a purged thread and "
        f"{unscoped_found} on an unscoped one. Kept after a failed reclaim: {kept_unclean}. "
        f"Reclaim failures recorded: {reported}. Containers left behind: {leftover}."
    )
    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(main()))
