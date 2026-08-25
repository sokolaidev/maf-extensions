"""When a sandbox goes away: within a turn, at the end of one, on thread delete — and unasked.

Four disposal moments. Choosing between the first three is a cost decision rather than a style
one; the fourth is not chosen at all, and is what the framework does about a call it could not
clean. Every other sample creates a sandbox and drops it on the way out; this one is about what
a long-lived host has to wire, because a sandbox is keyed by `(scope, thread_id, agent_dir)`
and outlives the turn that made it.

Needs a Docker-compatible engine. Containers are counted with `docker ps` rather than trusted
from a return value — except in act 5, which runs on the in-process backend because no real
one can be told to refuse a removal, and which says so where it prints. See this directory's
README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.25",
# ]
# ///

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from _scaffold import installed_versions
from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox.maf import (
    SandboxPurger,
    list_no_files,
    make_caller_context,
    sandboxed_tool,
)
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from maf_sandbox import ReclaimFailure
    from maf_sandbox.maf import SandboxToolSession

IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"
SCOPE = "samples"
AGENT_DIR = "assistant"

#: Act 5's thread. Kept apart from the four docker ones because that act runs on a different
#: backend, and `containers()` would answer 0 for it whatever happened.
UNCLEAN_THREAD = "t-unclean"

#: What act 5's tool call leaves in the sandbox — the data whose retention the whole act is
#: about, so it is worth being able to read it back by name.
NOTE = "left behind"

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
    """The spec acts 1 to 4 use. A function so each act states it rather than sharing one."""
    return SandboxSpec(kind="assistant", image=IMAGE)


def note_spec() -> SandboxSpec:
    """Act 5's, and it names no image: nothing there runs in a container."""
    return SandboxSpec(kind="note-taker")


async def one_turn(router: SandboxRouter, key: SandboxKey) -> None:
    """A turn: acquire, use, return. It does not dispose — that is the host's decision."""
    sandbox = await router.acquire(key, spec())
    await sandbox.write_file(
        f"{spec().work_dir}/turn", "worked\n", working_directory=spec().work_dir
    )
    await sandbox.exec("cat turn", working_directory=spec().work_dir, timeout=60)


async def act_one_reuse_within_a_turn(router: SandboxRouter) -> None:
    """What warm reuse actually buys, and the only place it is unambiguously worth having."""
    print("== 1. Within a turn: get-or-create is the point ==\n")

    key = SandboxKey(scope=SCOPE, thread_id="t-reuse", agent_dir=AGENT_DIR)

    # Tested as state surviving rather than with `is`: the protocol promises the same sandbox,
    # not the same object, and the docker backend hands back a fresh handle over one container.
    first = await router.acquire(key, spec())
    await first.write_file(
        f"{spec().work_dir}/from-first-acquire",
        "still here\n",
        working_directory=spec().work_dir,
    )

    second = await router.acquire(key, spec())
    read_back = await second.exec(
        "cat from-first-acquire", working_directory=spec().work_dir, timeout=60
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


# --- Act 5: the moment nobody chose ------------------------------------------------------------


class RefusingReclaim(InProcessSandbox):
    """An in-process sandbox that serves a call and then will not clean up after it.

    The one method overridden is the one the protocol makes mandatory. `Sandbox.reclaim` is what
    the framework calls on the call's own directory when the tool body returns, and a backend
    that cannot remove it raises: a read-only mount, a guest running as a user that does not own
    the path, a store that no longer has it. Everything else is `InProcessSandbox`'s behaviour.
    """

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        raise OSError(f"could not reclaim {directory}: rm exited 1")


def _note_body(session: SandboxToolSession) -> Callable[..., Awaitable[str]]:
    """The smallest tool that owns a guest path: it writes one line and says where."""

    async def take_note(text: str) -> str:
        """Write a note inside the sandbox.

        Args:
            text: The line to write.
        """
        key = session.key()
        if isinstance(key, str):
            return key
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            # The refusal a caller reads when the router will not serve the key — the third
            # outcome below, and the only one visible without a callback wired at all.
            return sandbox
        # Written inside the call's own directory, so the framework is what removes it. Or
        # tries to: the sandbox above refuses, which is the whole of what this act stages.
        path = f"{session.guest_call_path()}/note"
        await sandbox.write_file(path, text, working_directory=session.spec.work_dir)
        return path

    return take_note


def _wired(
    *, keep_unclean: bool = False, dispose_error: BaseException | None = None
) -> tuple[Any, SandboxRouter, InProcessSandboxBackend, list[ReclaimFailure]]:
    """A tool whose sandbox will not clean up, and the host wiring around it.

    The two things act 5 varies are both here and both the host's: whether it opted down from
    the framework disposing an unclean sandbox, and — for the third run — whether that disposal
    lands when it is attempted.
    """
    backend = InProcessSandboxBackend(RefusingReclaim(), dispose_error=dispose_error)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, keep_unclean=keep_unclean)
    heard: list[ReclaimFailure] = []

    async def told(failure: ReclaimFailure) -> None:
        # The host's handler, and all of it. It runs *after* the framework has acted, so there
        # is nothing for it to arrange — only what this host does with the fact. A real one
        # counts these and pages on `disposal == "failed"`.
        heard.append(failure)

    tool = sandboxed_tool(
        _note_body,
        router=router,
        context=make_caller_context(list_no_files, lambda: SCOPE, lambda: UNCLEAN_THREAD),
        agent_dir=AGENT_DIR,
        spec=note_spec(),
        name="take_note",
        on_reclaim_failure=told,
    )[0]
    return tool, router, backend, heard


async def act_five_a_call_that_could_not_be_cleaned() -> tuple[str, str, str]:
    """The fourth moment, and the only one no host chose: cleanup that did not work.

    Returns the three disposals the host was told about, so the footer reports what the run
    observed rather than what this file expects.
    """
    print("== 5. Cleanup that did not work: the moment nobody chose ==\n")
    print("  Not on Docker, and that is a finding rather than a shortcut. That backend")
    print("  reclaims with `rm -rf` running as root inside the container, and there is no")
    print("  honest way to make it refuse — so this act runs on the in-process backend, where")
    print("  the one member every backend must serve can be told to say no.\n")

    tool, router, backend, heard = _wired()
    await tool.invoke(arguments={"text": NOTE}, skip_parsing=True)
    default = heard[0].disposal
    print(
        f"  default posture       -> disposal={default}, and the backend was asked to "
        f"dispose it {len(backend.disposed)} time(s)"
    )
    await router.dispose_scope(SCOPE, UNCLEAN_THREAD)

    tool, router, backend, heard = _wired(keep_unclean=True)
    left = str(await tool.invoke(arguments={"text": NOTE}, skip_parsing=True))
    kept = heard[0].disposal
    warm = await router.acquire(
        SandboxKey(scope=SCOPE, thread_id=UNCLEAN_THREAD, agent_dir=AGENT_DIR), note_spec()
    )
    read_back = await warm.read_file(left, working_directory=note_spec().work_dir, max_bytes=64)
    print(
        f"\n  keep_unclean=True     -> disposal={kept}, and the backend was asked to "
        f"dispose it {len(backend.disposed)} time(s)"
    )
    print(f"  and the next acquire read the call's file back: {read_back.decode()!r}")
    await router.dispose_scope(SCOPE, UNCLEAN_THREAD)

    tool, router, backend, heard = _wired(dispose_error=RuntimeError("the backend is unreachable"))
    await tool.invoke(arguments={"text": NOTE}, skip_parsing=True)
    failed = heard[0].disposal
    refused = str(await tool.invoke(arguments={"text": NOTE}, skip_parsing=True))
    print(f"\n  a disposal that fails -> disposal={failed}, and the next call was refused:")
    print(f"    {refused!r}")

    print("\n  Three outcomes, one failure. The framework disposes what it could not clean")
    print("  before the host hears about it, because `acquire` is get-or-create and a sandbox")
    print("  left warm hands the next call everything the last one could not take back — the")
    print("  middle line above is that, read back through a later acquire. `keep_unclean=True`")
    print("  is the host's to set and no kind's, and it buys a warm sandbox at exactly that")
    print("  price. A disposal that does not land refuses the key until one does, which is a")
    print("  failed conversation rather than a leaked one.")
    print("\n  What this act cannot show is the disposed sandbox being cold afterwards: the")
    print("  in-process backend hands back the same object whatever was disposed, where a")
    print("  container backend creates a new one. So the first line reports the disposal the")
    print("  backend was actually asked for, which is the whole of that mechanism here — and")
    print("  the read above is the cost of opting down only because of the 0 beside it, since")
    print("  on this backend it would come back after a disposal too.\n")

    return default, kept, failed


async def main() -> int:
    """Five acts: four against one Docker backend counted with `docker ps`, and one that cannot
    be counted that way and says so."""
    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    try:
        await act_one_reuse_within_a_turn(router)
        await act_two_between_turns(router)
        await act_three_purge_at_end_of_turn(router)
        tidy_found, unscoped_found = await act_four_thread_delete(router)
        disposals = await act_five_a_call_that_could_not_be_cleaned()
    finally:
        # Whatever any act left behind, however it ended. The sample is about not leaking, so
        # it does not get to leak while saying so.
        for thread in ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped"):
            await router.dispose_scope(SCOPE, thread)

    leftover = sum(
        containers(thread) for thread in ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped")
    )
    print(
        f"Completed 5 of 5 acts. Purger found {tidy_found} on a purged thread and "
        f"{unscoped_found} on an unscoped one. The three unclean postures reported "
        f"{', '.join(disposals)}. Containers left behind: {leftover}."
    )
    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(main()))
