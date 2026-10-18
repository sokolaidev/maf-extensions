"""Measure Docker deletion authority and retained capability refusals in disposable guests."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shlex
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from maf_sandbox import Capability, IsolationScope, SandboxKey, SandboxSpec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

WORK = "/maf-sandbox/work"
GRANTS = ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL")
BITS = {"CHOWN": 0, "DAC_OVERRIDE": 1, "KILL": 5, "SETGID": 6, "SETUID": 7}
DOCKERFILE = """FROM {base}
RUN mkdir -p /guest-bin /protected /outside /maf-sandbox/work/call/parent && chmod 777 /guest-bin && chmod 700 /protected && echo sentinel > /protected/sentinel && echo sentinel > /outside/sentinel && echo target > /maf-sandbox/work/call/parent/sentinel
ENV PATH=/guest-bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
USER {user}
"""


def _docker(
    *args: str, input_text: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result


def _guest(name: str, code: str) -> str:
    return _docker("exec", name, "/usr/local/bin/python3", "-c", code).stdout.strip()


def _exists(name: str, path: str) -> bool:
    result = _docker(
        "exec",
        "--user",
        "0",
        name,
        "/usr/local/bin/python3",
        "-c",
        f"import json,os\ntry: os.lstat({path!r})\nexcept FileNotFoundError: exists=False\nelse: exists=True\nprint(json.dumps(exists))",
    )
    exists = json.loads(result.stdout)
    if not isinstance(exists, bool):
        raise ValueError("file observation did not return a boolean")
    return exists


def _container_present(instance_id: str) -> bool:
    inventory = _docker("ps", "-a", "--no-trunc", "--format", "{{.ID}}").stdout.splitlines()
    return instance_id in inventory


def _status(name: str) -> dict[str, str]:
    return json.loads(
        _guest(
            name,
            "import json; print(json.dumps({k:v.strip() for k,v in (l.split(':',1) for l in open('/proc/self/status') if ':' in l) if k in ('Uid','Gid','CapInh','CapPrm','CapEff','CapBnd','CapAmb','NoNewPrivs')}))",
        )
    )


def _command(name: str, body: str) -> None:
    _guest(
        name,
        f"from pathlib import Path; p=Path('/guest-bin/rm'); p.write_text({('#!/bin/sh\n' + body + '\n')!r}); p.chmod(0o755)",
    )


async def _operation(sandbox: Any, method: str, *, timeout: float = 10) -> None:
    if method == "remove":
        await sandbox.remove("parent/sentinel", working_directory=WORK + "/call")
    else:
        await sandbox.reclaim("parent/sentinel", working_directory=WORK + "/call", timeout=timeout)


async def _measure(
    image: str, label: str, method: str, grants: Sequence[str] = ()
) -> dict[str, Any]:
    backend = await DockerSandboxBackend.create(DockerSandboxConfig(cap_add=tuple(grants)))
    key = SandboxKey("deletion-audit-" + uuid.uuid4().hex, "thread", "agent", call_id="call")
    spec = SandboxSpec(
        kind="deletion-audit",
        image=image,
        isolation_scope=IsolationScope.CALL,
        requires=frozenset({Capability.EXEC}),
    )
    sandbox = await backend.acquire(key, spec)
    name = sandbox.container_name
    result: dict[str, Any] = {"case": label, "method": method, "grants": list(grants)}
    try:
        result["guest_status"] = _status(name)
        expected = sum(1 << BITS[g] for g in grants)
        for field in ("CapPrm", "CapEff", "CapBnd"):
            assert int(result["guest_status"][field], 16) == expected
        for field in ("CapInh", "CapAmb"):
            assert int(result["guest_status"][field], 16) == 0
        assert result["guest_status"]["NoNewPrivs"] == "1"
        result["declarations"] = sorted(str(c) for c in backend.declarations.capabilities)
        if label in {"reclaim_noncanonical", "remove_preflight"}:
            if label == "reclaim_noncanonical":
                _guest(
                    name,
                    "import os; os.makedirs('/guest-bin/base'); os.mkdir('/outside/child'); os.symlink('/outside/child','/guest-bin/base/link')",
                )
                _command(name, "exit 0")
                target = "/guest-bin/base/link/../sentinel"
                assert (
                    _guest(name, f"import os; print(os.path.realpath({target!r}))")
                    == "/outside/sentinel"
                )
                assert not _exists(name, "/guest-bin/base/sentinel")
            else:
                target = "../escape"
            original_run = sandbox._run
            calls = 0

            async def counted_preflight(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                calls += 1
                return await original_run(*args, **kwargs)

            sandbox._run = counted_preflight
            targets = [target, ".", WORK, "child/.."] if method == "remove" else [target]
            for rejected in targets:
                try:
                    if method == "remove":
                        await sandbox.remove(rejected, working_directory=WORK)
                    else:
                        await sandbox.reclaim(
                            rejected, working_directory="/guest-bin/base", timeout=30
                        )
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid path was accepted")
            result["targets_refused"] = targets
            assert calls == 0
            assert _exists(name, "/outside/sentinel")
            result["backend_io_calls"] = calls
            result["outcome"] = "refused_before_io"
        elif label == "refusal":
            assert Capability.FILES_DELETE not in backend.declarations.capabilities
            assert Capability.RECLAIM not in backend.declarations.capabilities
            _guest(
                name,
                "import os; os.mkdir('/maf-sandbox/work/call/original'); os.rename('/maf-sandbox/work/call/parent/sentinel','/maf-sandbox/work/call/original/sentinel'); os.rmdir('/maf-sandbox/work/call/parent'); os.symlink('/outside','/maf-sandbox/work/call/parent'); os.chmod('/maf-sandbox',0o777)",
            )
            _command(name, "exit 0")
            original_run = sandbox._run
            calls = 0

            async def counted(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                calls += 1
                return await original_run(*args, **kwargs)

            sandbox._run = counted
            try:
                await _operation(sandbox, method)
            except NotImplementedError:
                result["outcome"] = "refused"
            else:
                raise AssertionError("nonempty grants allowed deletion")
            assert calls == 0
            result["backend_io_calls"] = calls
            assert _exists(name, "/outside/sentinel")
            assert _exists(name, WORK + "/call/original/sentinel")
            result["sentinels_preserved"] = True
        elif label == "guest_owned":
            _guest(name, "open('/guest-bin/target','w').write('target')")
            if method == "remove":
                await sandbox.remove("target", working_directory="/guest-bin")
            else:
                await sandbox.reclaim("target", working_directory="/guest-bin", timeout=30)
            assert not _exists(name, "/guest-bin/target")
            result["outcome"] = "guest_owned_target_removed"
        elif label == "parent_swap":
            result["guest_can_delete_outside"] = (
                _guest(
                    name,
                    "import os; os.unlink('/outside/sentinel'); open('/outside/sentinel','w').write('sentinel'); print('yes')",
                )
                == "yes"
            )
            original_removal = sandbox._exec

            async def swapped(*args: Any, **kwargs: Any) -> Any:
                _guest(
                    name,
                    "import os; os.mkdir('/maf-sandbox/work/call/original'); os.rename('/maf-sandbox/work/call/parent/sentinel','/maf-sandbox/work/call/original/sentinel'); os.rmdir('/maf-sandbox/work/call/parent'); os.symlink('/outside','/maf-sandbox/work/call/parent'); os.chmod('/maf-sandbox',0o777)",
                )
                return await original_removal(*args, **kwargs)

            sandbox._exec = swapped
            try:
                await _operation(sandbox, method)
            except ValueError as exc:
                assert "link rather than a real directory" in str(exc)
            else:
                raise AssertionError("completion accepted a swapped ancestor")
            assert not _exists(name, "/outside/sentinel")
            assert result["guest_can_delete_outside"]
            result["outcome"] = "outside_deleted_within_guest_authority_completion_refused"
        elif label in {"private_parent_present", "private_parent_missing"}:
            leaf = "sentinel" if label == "private_parent_present" else "missing"
            control = _guest(
                name,
                f"import os\ntry: os.stat('/protected/{leaf}')\nexcept PermissionError: print('denied')\nelse: print('visible')",
            )
            assert control == "denied"
            _guest(
                name,
                f"import os; os.mkdir('/guest-bin/parent'); open('/guest-bin/parent/{leaf}','w').close()",
            )
            swap = f"import os; os.unlink('/guest-bin/parent/{leaf}'); os.rmdir('/guest-bin/parent'); os.symlink('/protected','/guest-bin/parent')"
            _command(name, f"/usr/local/bin/python3 -c {shlex.quote(swap)}")
            try:
                if method == "remove":
                    await sandbox.remove(f"parent/{leaf}", working_directory="/guest-bin")
                else:
                    await sandbox.reclaim(
                        f"parent/{leaf}", working_directory="/guest-bin", timeout=30
                    )
            except ValueError as exc:
                assert "link rather than a real directory" in str(exc)
            else:
                raise AssertionError("completion exposed a root-private target")
            assert _exists(name, "/protected/sentinel")
            result["guest_stat_control"] = control
            result["outcome"] = "private_parent_refused"
        elif label == "command_authority":
            control = _guest(
                name,
                "import os\ntry: os.unlink('/protected/sentinel')\nexcept PermissionError: print('denied')\nelse: print('deleted')",
            )
            assert control == "denied"
            _command(
                name,
                '/usr/local/bin/python3 -c "import os; os.unlink(\'/protected/sentinel\')"\nexec /bin/rm "$@"',
            )
            try:
                await _operation(sandbox, method)
            except OSError:
                pass
            else:
                raise AssertionError("guest cannot remove the root-owned target")
            assert _exists(name, "/protected/sentinel")
            result["guest_delete_control"] = control
            result["outcome"] = "guest_inaccessible_sentinel_preserved"
        elif label == "false_success":
            _command(name, "exit 0")
            try:
                await _operation(sandbox, method)
            except OSError:
                pass
            else:
                raise AssertionError("false success was accepted")
            assert _exists(name, WORK + "/call/parent/sentinel")
            result["outcome"] = "surviving_target_refused"
        elif label == "failure":
            _command(name, "exit 23")
            try:
                await _operation(sandbox, method)
            except OSError:
                result["outcome"] = "OSError"
            else:
                raise AssertionError("failed command reported success")
            assert _exists(name, WORK + "/call/parent/sentinel")
        elif label in {"timeout", "cancellation"}:
            _command(name, "echo started > /tmp/deletion-started\nexec /bin/sleep 60")
            if label == "timeout":
                original_exec = sandbox._exec

                async def bounded(*args: Any, **kwargs: Any) -> Any:
                    kwargs["timeout"] = 1
                    return await original_exec(*args, **kwargs)

                sandbox._exec = bounded
                try:
                    await _operation(sandbox, method, timeout=1)
                except TimeoutError:
                    result["outcome"] = "TimeoutError"
                else:
                    raise AssertionError("blocked command did not time out")
                result["container_present_after_operation"] = _container_present(
                    sandbox.instance_id
                )
                assert not result["container_present_after_operation"]
            else:
                executing = asyncio.Event()
                original_exec = sandbox._exec

                async def entered_exec(*args: Any, **kwargs: Any) -> Any:
                    executing.set()
                    return await original_exec(*args, **kwargs)

                sandbox._exec = entered_exec
                task = asyncio.create_task(_operation(sandbox, method, timeout=60))
                try:
                    async with asyncio.timeout(20):
                        await executing.wait()
                        while not _exists(name, "/tmp/deletion-started"):
                            await asyncio.sleep(0.05)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        result["outcome"] = "CancelledError"
                    else:
                        raise AssertionError("cancellation did not propagate")
                result["container_present_after_operation"] = _container_present(
                    sandbox.instance_id
                )
                assert result["container_present_after_operation"]
        else:
            raise ValueError(label)
    finally:
        assert await backend.dispose(key, kind=spec.kind, instance_id=sandbox.instance_id) is None
        assert not _container_present(sandbox.instance_id)
        result["independent_disposal_check"] = "absent"
    print(json.dumps(result), flush=True)
    return result


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", default="python:3.13-slim")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    base = json.loads(_docker("image", "inspect", args.base_image).stdout)[0]
    images = {}
    for user in ("0", "1000:1000"):
        built = _docker(
            "build", "-q", "-", input_text=DOCKERFILE.format(base=args.base_image, user=user)
        )
        images[user] = built.stdout.strip().splitlines()[-1]
    root = Path(__file__).resolve().parents[2]
    sources = [
        "packages/maf-sandbox-docker/src/maf_sandbox_docker/_backend.py",
        "packages/maf-sandbox/src/maf_sandbox/_protocol.py",
        "scripts/experiments/docker_deletion_probe.py",
    ]
    report: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
        "host_os": platform.system(),
        "python": platform.python_version(),
        "package_version": version("maf-sandbox-docker"),
        "docker_version": json.loads(_docker("version", "--format", "{{json .}}").stdout),
        "base_image": {k: base[k] for k in ("Id", "RepoDigests", "Architecture", "Os")},
        "fixture_dockerfile": DOCKERFILE,
        "fixture_images": images,
        "results": [],
    }
    cases = []
    for mask in range(1, 32):
        grants = tuple(g for i, g in enumerate(GRANTS) if mask & (1 << i))
        for method in ("remove", "reclaim"):
            cases.append((images["0"], "refusal", method, grants))
    for label in (
        "parent_swap",
        "private_parent_present",
        "private_parent_missing",
        "command_authority",
        "false_success",
        "failure",
        "timeout",
        "cancellation",
        "guest_owned",
    ):
        for method in ("remove", "reclaim"):
            cases.append(
                (
                    images["1000:1000"]
                    if label
                    in {
                        "command_authority",
                        "guest_owned",
                        "private_parent_present",
                        "private_parent_missing",
                    }
                    else images["0"],
                    label,
                    method,
                    (),
                )
            )
    cases.extend(
        [
            (images["0"], "reclaim_noncanonical", "reclaim", ()),
            (images["0"], "remove_preflight", "remove", ()),
        ]
    )
    try:
        for start in range(0, len(cases), args.jobs):
            batch = await asyncio.gather(
                *(_measure(*case) for case in cases[start : start + args.jobs]),
                return_exceptions=True,
            )
            for result in batch:
                if isinstance(result, BaseException):
                    report.setdefault("errors", []).append(
                        type(result).__name__ + ": " + str(result)
                    )
                else:
                    report["results"].append(result)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        assert not report.get("errors"), report.get("errors")
        report["complete"] = True
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(_main())
