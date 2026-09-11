"""Image prerequisites are checked before handing out a usable sandbox."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest
from maf_sandbox import Capability, SandboxCapabilityNotSupported, SandboxKey, SandboxSpec


@pytest.mark.parametrize("backend", ["docker", "wslc", "acas"])
def test_only_requested_commands_are_probed_and_success_is_reused(backend):
    check = import_module(f"maf_sandbox_{backend}._probes").probe_commands

    async def scenario():
        seen = []
        verified = set()

        async def run(argv, as_root, owns_capture=False):
            seen.append((argv, as_root, owns_capture))
            return 0

        await check(SandboxSpec(kind="probe", requires=frozenset()), verified, run)
        assert not seen
        spec = SandboxSpec(kind="probe", requires=frozenset({Capability.EXEC}))
        await check(spec, verified, run)
        await check(spec, verified, run)
        assert seen[0] == (("sh", "-c", "exit 0"), False, False)
        assert len(seen) == (2 if backend == "acas" else 1)
        if backend == "acas":
            assert "mkfifo" in seen[1][0][2] and "head -c" in seen[1][0][2]
            assert seen[1][1:] == (False, True)

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["docker", "wslc", "acas"])
@pytest.mark.parametrize("failure", [127, TimeoutError(), OSError("unreachable")])
def test_unsuccessful_checks_are_typed_refusals_and_retry(backend, failure):
    check = import_module(f"maf_sandbox_{backend}._probes").probe_commands

    async def scenario():
        verified = set()
        spec = SandboxSpec(kind="probe", image="guest:tag", requires=frozenset({Capability.EXEC}))

        async def fail(argv, as_root, _owns_capture=False):
            if isinstance(failure, Exception):
                raise failure
            return failure

        with pytest.raises(SandboxCapabilityNotSupported, match="exec.*probe.*guest:tag.*sh"):
            await check(spec, verified, fail)
        assert not verified

        async def succeed(argv, as_root, _owns_capture=False):
            return 0

        await check(spec, verified, succeed)
        assert verified == ({"sh", "exec-capture"} if backend == "acas" else {"sh"})

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["docker", "wslc", "acas"])
def test_cancellation_is_not_a_verdict(backend):
    check = import_module(f"maf_sandbox_{backend}._probes").probe_commands

    async def scenario():
        verified = set()

        async def cancel(argv, as_root, _owns_capture=False):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await check(SandboxSpec(kind="probe"), verified, cancel)
        assert not verified

    asyncio.run(scenario())


def test_docker_file_only_work_needs_no_guest_command_and_delete_needs_no_shell():
    from maf_sandbox_docker._probes import probe_commands

    async def scenario():
        seen = []

        async def run(argv, as_root, _owns_capture=False):
            seen.append((argv, as_root))
            return 0

        await probe_commands(
            SandboxSpec(
                kind="files", requires=frozenset({Capability.FILES_IN, Capability.FILES_OUT})
            ),
            set(),
            run,
        )
        assert not seen
        await probe_commands(
            SandboxSpec(kind="delete", requires=frozenset({Capability.FILES_DELETE})),
            set(),
            run,
        )
        assert seen == [(("rm", "-rf", "--"), False)]

    asyncio.run(scenario())


@pytest.mark.parametrize("negative_status", [0, 1, 127])
def test_wslc_tests_the_external_binary_with_true_and_false_cases(negative_status):
    from maf_sandbox_wslc._probes import probe_commands

    async def scenario():
        seen = []
        verified = set()

        async def run(argv, as_root, _owns_capture=False):
            seen.append((argv, as_root))
            return negative_status if argv[1] == "-e" else 0

        spec = SandboxSpec(kind="write", requires=frozenset({Capability.FILES_IN}))
        if negative_status == 1:
            await probe_commands(spec, verified, run)
            assert verified == {"test"}
        else:
            with pytest.raises(SandboxCapabilityNotSupported, match="files_in.*test"):
                await probe_commands(spec, verified, run)
            assert not verified
        assert seen[0] == (("test", "-d", "/"), True)
        assert seen[1][0][:2] == ("test", "-e")
        assert seen[1][1] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["docker", "acas"])
@pytest.mark.parametrize("status", [0, 11, 12, 13, 127])
def test_host_tools_batches_required_utilities_and_keeps_optional_runtime_choices(backend, status):
    check = import_module(f"maf_sandbox_{backend}._probes").probe_commands

    async def scenario():
        seen = []
        verified = set()

        async def run(argv, as_root, owns_capture=False):
            seen.append((argv, owns_capture))
            assert not as_root
            return status

        spec = SandboxSpec(
            kind="host", requires=frozenset({Capability.EXEC, Capability.HOST_TOOLS})
        )
        if status == 0:
            await check(spec, verified, run)
            await check(spec, verified, run)
            assert verified == (
                {"sh", "host-tools", "exec-capture"} if backend == "acas" else {"sh", "host-tools"}
            )
        else:
            with pytest.raises(SandboxCapabilityNotSupported, match="host_tools"):
                await check(spec, verified, run)
            assert not verified
        assert len(seen) == (2 if backend == "acas" and status == 0 else 1)
        assert seen[0][1] is False
        if backend == "acas" and status == 0:
            assert seen[1][1] is True
        assert seen[0][0][:2] == ("sh", "-c")
        script = seen[0][0][2]
        assert all(command in script for command in ("mkdir", "mv", "nohup"))
        assert "setsid" not in script and "python" not in script

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["docker", "wslc"])
def test_engine_probe_cache_is_per_instance_and_extends_for_new_requirements(kind):
    package = import_module(f"maf_sandbox_{kind}")
    backend_type = getattr(package, f"{kind.capitalize()}SandboxBackend")
    config_type = getattr(package, f"{kind.capitalize()}SandboxConfig")
    backend = backend_type(config_type())
    calls = []

    async def engine(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1 if "-e" in args else 0)

    setattr(backend, f"_{kind}", engine)

    async def scenario():
        spec = SandboxSpec(kind="probe", image="mutable:tag", requires=frozenset())
        declarations = backend.declarations
        await backend._probe_commands("same-name", "first-id", spec)
        assert not calls
        shell = SandboxSpec(
            kind="probe", image="mutable:tag", requires=frozenset({Capability.EXEC})
        )
        await backend._probe_commands("same-name", "first-id", shell)
        await backend._probe_commands("same-name", "first-id", shell)
        assert len(calls) == 1
        capability = Capability.FILES_DELETE if kind == "docker" else Capability.FILES_IN
        richer = SandboxSpec(kind="probe", requires=frozenset({Capability.EXEC, capability}))
        await backend._probe_commands("same-name", "first-id", richer)
        assert len(calls) == (2 if kind == "docker" else 3)
        await backend._probe_commands("same-name", "replacement-id", shell)
        assert "replacement-id" in calls[-1][0]
        assert backend.declarations is declarations
        assert all(0 < options["timeout"] <= 10 for _, options in calls)
        assert all(options["read_limit"] == 1024 for _, options in calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["docker", "wslc"])
def test_an_engine_that_ignores_its_timeout_is_still_bounded(kind):
    package = import_module(f"maf_sandbox_{kind}")
    backend_type = getattr(package, f"{kind.capitalize()}SandboxBackend")
    config_type = getattr(package, f"{kind.capitalize()}SandboxConfig")
    backend = backend_type(config_type(command_timeout_seconds=0.01))

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    setattr(backend, f"_{kind}", hang)

    async def scenario():
        with pytest.raises(SandboxCapabilityNotSupported, match="did not complete"):
            await asyncio.wait_for(
                backend._probe_commands("name", "id", SandboxSpec(kind="probe")),
                timeout=1,
            )
        assert not backend._command_probes["name"][1]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["docker", "wslc"])
@pytest.mark.parametrize("scope", ["key", "conversation", "instance"])
def test_disposal_releases_probe_cache_even_when_the_instance_is_already_absent(kind, scope):
    package = import_module(f"maf_sandbox_{kind}")
    implementation = import_module(f"maf_sandbox_{kind}._backend")
    backend = getattr(package, f"{kind.capitalize()}SandboxBackend")(
        getattr(package, f"{kind.capitalize()}SandboxConfig")()
    )
    key = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    backend._registry[(key.scope, key.thread_id, key.agent_dir, "probe")] = "name"
    backend._command_probes["name"] = ("instance", {"sh"})
    backend._command_probes["sibling"] = ("other-instance", {"sh"})

    async def absent(*args, **kwargs):
        return None

    async def empty_sweep(*args, **kwargs):
        return implementation._Sweep(0)

    backend._inspect_disposal_target = absent
    backend._purge = empty_sweep

    async def scenario():
        if scope == "conversation":
            await backend.dispose_scope(key.scope, key.thread_id)
        elif scope == "instance":
            await backend.dispose(key, instance_id="instance")
        else:
            await backend.dispose(key)
        assert backend._command_probes == {"sibling": ("other-instance", {"sh"})}

    asyncio.run(scenario())
