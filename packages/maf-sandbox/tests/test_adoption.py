"""Unfamiliar engine instances are cleaned before a router serves them."""

import asyncio
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from maf_sandbox import (
    Capability,
    Cleanup,
    DisposalFailure,
    FailedReclaimPolicy,
    Isolation,
    IsolationScope,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxUnclean,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)

KEY = SandboxKey(scope="s", thread_id="t", agent_dir="a")
SPEC = SandboxSpec(kind="test", confined_to_guest_call_path=True)


def backend(*, snapshot=False):
    return InProcessSandboxBackend(
        sandbox_per_key=True,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities
            | ({Capability.SNAPSHOT} if snapshot else set()),
            isolation_scopes=frozenset(IsolationScope),
        ),
    )


def router(subject, *, keep=False):
    return SandboxRouter(
        [subject],
        min_isolation=Isolation.NONE,
        reclaim=ReclaimConfig(
            timeout=0.02,
            failed_reclaim_policy=FailedReclaimPolicy.KEEP if keep else FailedReclaimPolicy.DISPOSE,
        ),
    )


@pytest.mark.parametrize("snapshot", [False, True])
def test_restart_cleans_unknown_files_and_processes_once(snapshot):
    subject = backend(snapshot=snapshot)

    async def scenario():
        old = await subject.acquire(KEY, SPEC)
        old.contents["/tmp/unknown"] = b"residue"
        old.running.add("survivor")
        old_id = old.instance_id
        current = router(subject)
        adopted = await current.acquire(KEY, SPEC)
        assert isinstance(adopted, InProcessSandbox)
        assert adopted.instance_id != old_id
        assert not adopted.contents and not adopted.running
        assert not adopted.reclaims
        count = len(subject.keys)
        again = await current.acquire(KEY, SPEC)
        assert again.instance_id == adopted.instance_id
        assert len(subject.keys) == count + 1
        assert len(subject.disposed) == (0 if snapshot else 1)
        assert len(adopted.resets) == (1 if snapshot else 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["adoption", "cleanup"])
@pytest.mark.parametrize("value", ["unchanged", None, "", 7])
def test_reset_must_establish_a_new_valid_identity(phase, value):
    subject = backend(snapshot=True)
    current = router(subject)

    async def scenario():
        sandbox = subject.sandbox
        if phase == "cleanup":
            sandbox = await current.acquire(KEY, SPEC)
        assert isinstance(sandbox, InProcessSandbox)

        async def reset(*, timeout):
            if value != "unchanged":
                sandbox.instance_id = value

        sandbox.reset = reset
        if phase == "adoption":
            await current.acquire(KEY, SPEC)
        else:
            await current._run_the_rung(KEY, SPEC, subject, Cleanup.RESET, sandbox, None, 1)
        assert subject.disposed == [KEY]

    asyncio.run(scenario())


def test_reset_retires_only_the_replaced_instance():
    subject = backend(snapshot=True)
    current = router(subject)
    other_policy = InProcessSandbox()
    at = (KEY, SPEC.kind, id(subject))

    async def scenario():
        sandbox = await current.acquire(KEY, SPEC)
        current._remember_instance(KEY, SPEC.kind, subject, other_policy)
        for _ in range(4):
            await current._run_the_rung(KEY, SPEC, subject, Cleanup.RESET, sandbox, None, 1)
            assert current._seen[at] == {sandbox.instance_id, other_policy.instance_id}

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["kind", "key", "scope", "cleanup"])
@pytest.mark.parametrize("fails", [False, True])
def test_disposal_retires_only_covered_instances_on_success(operation, fails):
    subject = backend(snapshot=True)
    current = router(subject)
    other_backend = backend()
    entries = [
        (KEY, SPEC.kind, subject),
        (KEY, "sibling", subject),
        (dataclasses.replace(KEY, agent_dir="other"), SPEC.kind, subject),
        (dataclasses.replace(KEY, thread_id="other"), SPEC.kind, subject),
        (KEY, SPEC.kind, other_backend),
    ]
    for key, kind, provider in entries:
        current._remember_instance(key, kind, provider, InProcessSandbox())
    before = dict(current._seen)
    if fails:
        subject.dispose_failure = subject.purge_failure = DisposalFailure("refused", "busy")

    async def scenario():
        if operation == "kind":
            await current.dispose_kind(KEY, SPEC.kind, timeout=1)
        elif operation == "key":
            await current.dispose(KEY)
        elif operation == "scope":
            await current.dispose_scope(KEY.scope, KEY.thread_id)
        else:
            await current._dispose_the_kind(KEY, SPEC, subject, None, 1)

    asyncio.run(scenario())
    removed = 0 if fails else {"kind": 1, "cleanup": 1, "key": 2, "scope": 3}[operation]
    expected = dict(before)
    for key, kind, provider in entries[:removed]:
        expected.pop((key, kind, id(provider)))
    assert current._seen == expected


def test_first_create_is_followed_by_one_disposal_and_one_fresh_acquire():
    subject = backend()
    asyncio.run(router(subject).acquire(KEY, SPEC))
    assert len(subject.keys) == 2
    assert subject.disposed == [KEY]
    assert subject.disposed_kinds == [SPEC.kind]


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("failure", ["refused", "timeout", "raise", "cancel"])
def test_adoption_failure_is_bounded_and_obeys_keep(keep, failure):
    subject = backend()
    attempted = []

    async def dispose(key, *, kind=None):
        attempted.append((key, kind))
        assert subject.keys == [KEY]
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "raise":
            raise RuntimeError("delete failed")
        if failure == "cancel":
            raise asyncio.CancelledError
        return DisposalFailure("refused", "delete failed")

    subject.dispose = dispose
    current = router(subject, keep=keep)

    async def scenario():
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await current.acquire(KEY, SPEC)
        elif keep:
            assert await current.acquire(KEY, SPEC) is subject.sandbox
            assert await current.acquire(KEY, SPEC) is subject.sandbox
        else:
            with pytest.raises(SandboxUnclean):
                await current.acquire(KEY, SPEC)
            with pytest.raises(SandboxUnclean):
                await current.acquire(KEY, SPEC)
        assert (KEY in current._unclean) is (not keep)
        assert len(attempted) == 1
        assert not current._adoptions._slots

    asyncio.run(scenario())


def test_call_scope_skips_adoption():
    subject = backend()
    key = dataclasses.replace(KEY, call_id="unique")
    spec = dataclasses.replace(SPEC, isolation_scope=IsolationScope.CALL)
    asyncio.run(router(subject).acquire(key, spec))
    assert subject.keys == [key]
    assert not subject.disposed


def test_concurrent_first_acquires_wait_for_one_adoption():
    subject = backend()
    entered, release = asyncio.Event(), asyncio.Event()
    original = subject.dispose

    async def dispose(key, *, kind=None):
        entered.set()
        await release.wait()
        return await original(key, kind=kind)

    subject.dispose = dispose
    current = router(subject)

    async def scenario():
        first = asyncio.create_task(current.acquire(KEY, SPEC))
        await entered.wait()
        second = asyncio.create_task(current.acquire(KEY, SPEC))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a.instance_id == b.instance_id
        assert len(subject.disposed) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["raise", "timeout"])
def test_reset_failure_falls_back_to_disposal(failure):
    subject = backend(snapshot=True)

    async def reset(*, timeout):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("baseline unavailable")

    subject.sandbox.reset = reset
    asyncio.run(router(subject).acquire(KEY, SPEC))
    assert len(subject.keys) == 2
    assert subject.disposed == [KEY]


def test_new_wrappers_share_engine_identity_and_replacements_are_adopted(monkeypatch):
    subject = backend()
    acquire = subject.acquire

    class Wrapper:
        def __init__(self, sandbox):
            self.sandbox = sandbox

        def __getattr__(self, name):
            return getattr(self.sandbox, name)

    async def wrapped(key, spec):
        return Wrapper(await acquire(key, spec))

    monkeypatch.setattr(subject, "acquire", wrapped)
    current = router(subject)

    async def scenario():
        first = await current.acquire(KEY, SPEC)
        again = await current.acquire(KEY, SPEC)
        assert first is not again
        assert first.instance_id == again.instance_id
        assert len(subject.disposed) == 1
        await subject.dispose(KEY, kind=SPEC.kind)
        replacement = await acquire(KEY, SPEC)
        replacement.contents["/tmp/residue"] = b"unknown"
        fresh = await current.acquire(KEY, SPEC)
        assert fresh.instance_id != replacement.instance_id
        assert not subject.sandboxes[(KEY, SPEC.kind)].contents
        assert len(subject.disposed) == 3

    asyncio.run(scenario())


def test_successful_end_of_call_reset_is_already_known():
    subject = backend(snapshot=True)
    current = router(subject)
    spec = dataclasses.replace(SPEC, confined_to_guest_call_path=False)

    async def scenario():
        admission = await current.enter_call(KEY, spec, owner="call")
        sandbox = await current.acquire(KEY, spec, _admission=admission)
        assert isinstance(sandbox, InProcessSandbox)
        await current.finish_call(KEY, spec, admission=admission, sandbox=sandbox, owner="call")
        assert len(sandbox.resets) == 2
        await current.acquire(KEY, SPEC)
        assert len(sandbox.resets) == 2

    asyncio.run(scenario())


def test_first_acquires_on_different_event_loops_share_one_adoption():
    subject = backend()
    entered, release = threading.Event(), threading.Event()
    original = subject.dispose

    async def dispose(key, *, kind=None):
        entered.set()
        assert await asyncio.to_thread(release.wait, 2)
        return await original(key, kind=kind)

    subject.dispose = dispose
    current = SandboxRouter(
        [subject], min_isolation=Isolation.NONE, reclaim=ReclaimConfig(timeout=3)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(asyncio.run, current.acquire(KEY, SPEC))
        assert entered.wait(2)
        second = pool.submit(asyncio.run, current.acquire(KEY, SPEC))
        release.set()
        assert first.result(3).instance_id == second.result(3).instance_id
    assert len(subject.disposed) == 1
    assert not current._adoptions._slots


@pytest.mark.parametrize("value", [None, "", 7])
def test_missing_or_invalid_instance_id_is_refused_and_disposed(value, monkeypatch):
    subject = backend()
    monkeypatch.setattr(subject.sandbox, "instance_id", value)
    with pytest.raises(TypeError, match="instance_id"):
        asyncio.run(router(subject).acquire(KEY, SPEC))
    assert subject.disposed == [KEY]


def test_cancelled_adoption_waiter_does_not_cancel_the_adoption():
    subject = backend()
    entered, release = asyncio.Event(), asyncio.Event()
    original = subject.dispose

    async def dispose(key, *, kind=None):
        entered.set()
        await release.wait()
        return await original(key, kind=kind)

    subject.dispose = dispose
    current = router(subject)

    async def scenario():
        first = asyncio.create_task(current.acquire(KEY, SPEC))
        await entered.wait()
        second = asyncio.create_task(current.acquire(KEY, SPEC))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        sandbox = await first
        assert (await current.acquire(KEY, SPEC)).instance_id == sandbox.instance_id
        assert len(subject.disposed) == 1
        assert not current._adoptions._slots

    asyncio.run(scenario())
