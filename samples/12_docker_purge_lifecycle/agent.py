"""When a sandbox goes away: within a turn, at the end of one, and when a thread is deleted.

Three disposal moments, and choosing between them is a cost decision rather than a style one.
Every other sample creates a sandbox and drops it on the way out; this one is about what a
long-lived host has to wire, because a sandbox is keyed by `(scope, thread_id, agent_dir)` and
outlives the turn that made it.

Needs a Docker-compatible engine. Containers are counted with `docker ps` rather than trusted
from a return value — see this directory's README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.14",
# ]
# ///

from __future__ import annotations

import asyncio
import subprocess

from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox.maf import SandboxPurger
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"
SCOPE = "samples"
AGENT_DIR = "assistant"

#: The labels `DockerSandboxBackend` stamps on every container it creates, and the same ones its
#: `dispose_scope` selects on. Short plain values pass through unchanged, which is why the
#: thread ids below are short and plain: it keeps `docker ps` a readable check rather than a
#: digest lookup.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"


def containers(thread_id: str) -> int:
    """How many containers Docker itself reports for ``thread_id`` — the outside view.

    The counts the library returns say what it believes it disposed. This says what is actually
    on the machine, which is the only evidence that means anything for a leak.

    ``-a``, so stopped containers count. Without it a purge that stopped a container without
    removing it would leave a labelled container on the machine while every count here read
    zero — the leak this sample exists to rule out, invisible to the check that rules it out.
    The backend lists the same way when it purges (``docker ps -a``), so this asks the question
    it answers.
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
    await sandbox.write_file(f"{spec().work_dir}/turn", "worked\n")
    await sandbox.exec("cat turn", working_directory=spec().work_dir, timeout=60)


async def act_one_reuse_within_a_turn(router: SandboxRouter) -> None:
    """What warm reuse actually buys, and the only place it is unambiguously worth having."""
    print("== 1. Within a turn: get-or-create is the point ==\n")

    key = SandboxKey(scope=SCOPE, thread_id="t-reuse", agent_dir=AGENT_DIR)

    # Tested as state surviving rather than with `is`: the protocol promises the same sandbox,
    # not the same object, and the docker backend hands back a fresh handle over one container.
    first = await router.acquire(key, spec())
    await first.write_file(f"{spec().work_dir}/from-first-acquire", "still here\n")

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
    print("  On ACAS the same choice is a bill. `AcasSandboxConfig` defaults to")
    print("  `auto_suspend_seconds=60` and `auto_delete_seconds=600`, so a sandbox held")
    print("  between turns is billable while it idles and gone ten minutes later regardless.")
    print("  If turns are minutes apart that trade can pay. If they are hours or days apart —")
    print("  which is what a real conversation looks like — it never does: the sandbox is")
    print("  auto-deleted long before the next turn, so the idle window is paid for and the")
    print("  reuse it was bought for never happens.\n")

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
    tidy_found = await purger.purge_scoped_thread(SCOPE, tidy)
    print(f"  a thread already purged per turn -> purger found {tidy_found}")
    print("  Zero is the right answer, not a broken hook. A host that purges at end of turn")
    print("  should expect the delete path to find nothing almost every time.\n")

    # A turn that ran with no `router.scope` around it — a host that never wired per-turn
    # disposal, or a sandbox left by a worker that died before it could. Note what this is
    # *not*: an abandoned `async with`. That block disposes however it exits, so a scope once
    # entered cannot orphan anything, and the only way to reach a delete with work outstanding
    # is never to have entered one.
    unscoped = "t-unscoped"
    await one_turn(router, SandboxKey(scope=SCOPE, thread_id=unscoped, agent_dir=AGENT_DIR))
    print(f"  a thread never scoped per turn -> containers: {containers(unscoped)}")
    unscoped_found = await purger.purge_scoped_thread(SCOPE, unscoped)
    print(f"  user deletes the conversation  -> purger found {unscoped_found}")
    print(f"  and docker agrees              -> containers: {containers(unscoped)}")
    print("  Nothing else would have reclaimed this one, and no turn is coming back for it.")
    print("  `dispose_scope` selects on the labels the backend stamped rather than on anything")
    print("  this process remembers, which is what lets the delete path reclaim sandboxes a")
    print("  replica never created — a crashed worker's, or an older deployment's. After it")
    print("  there is only the backend's own timer: on ACAS, ten minutes of billing away.\n")

    return tidy_found, unscoped_found


async def main() -> int:
    """Four acts against one Docker backend, counted with `docker ps` throughout."""
    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    try:
        await act_one_reuse_within_a_turn(router)
        await act_two_between_turns(router)
        await act_three_purge_at_end_of_turn(router)
        tidy_found, unscoped_found = await act_four_thread_delete(router)
    finally:
        # Whatever any act left behind, however it ended. The sample is about not leaking, so
        # it does not get to leak while saying so.
        for thread in ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped"):
            await router.dispose_scope(SCOPE, thread)

    leftover = sum(
        containers(thread) for thread in ("t-reuse", "t-kept", "t-perturn", "t-tidy", "t-unscoped")
    )
    print(
        f"Completed 4 of 4 acts. Purger found {tidy_found} on a purged thread and "
        f"{unscoped_found} on an unscoped one. Containers left behind: {leftover}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
