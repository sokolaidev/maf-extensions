"""Real launcher receipts protect unrelated processes even when guest PID files are forged."""

import asyncio
import json
import os
import posixpath
import shlex
import shutil
import uuid

import maf_sandbox
import maf_sandbox._host_tools_over_exec as transport
import pytest

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerSandbox

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker") or not hasattr(maf_sandbox, "ProcessesObserved"),
    reason="needs a Python Docker image and the process observation seam",
)


class Records(maf_sandbox.SandboxObserver):
    def __init__(self):
        self.snapshots = []
        self.signals = []

    def processes_observed(self, event):
        self.snapshots.append(event)

    def process_cleanup(self, event):
        self.signals.append(event)


@pytest.mark.parametrize("finish", [True, False], ids=["success", "timeout"])
@pytest.mark.parametrize("degraded", [None, "unavailable", "incomplete"])
@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_forged_files_do_not_redirect_cleanup_and_observed_escapees_are_stopped(
    finish, degraded, relative, monkeypatch
):
    generate = transport._launcher_script

    def delayed_receipt(*args, **kwargs):
        script = generate(*args, **kwargs)
        assert "maf_pid=$!; " in script
        return script.replace("maf_pid=$!; ", "maf_pid=$!; sleep 1; ").replace(
            "exec >/dev/null 2>&1;", "sleep 1; exec >/dev/null 2>&1;"
        )

    monkeypatch.setattr(transport, "_launcher_script", delayed_receipt)

    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig())
        name = "maf-process-test-" + uuid.uuid4().hex
        created = await backend._docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "64",
            "--memory",
            "128m",
            _IMAGE,
            "sleep",
            "120",
            timeout=30,
        )
        assert created.returncode == 0, created.stderr
        sandbox = _DockerSandbox(
            backend._docker,
            name,
            30,
            cap_drop_all=True,
            guest_uid=65534,
            guest_gid=65534,
            instance_id=created.stdout.decode().strip(),
            freeze=backend._freeze(name),
        )
        sandbox._work_dir = "/tmp"
        try:
            victim = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    (
                        "import subprocess; p=subprocess.Popen(['sleep','90'], "
                        + "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                        + "stderr=subprocess.DEVNULL); print(p.pid)"
                    ),
                ],
                working_directory="/tmp",
                timeout=10,
            )
            assert victim.exit_code == 0, victim.stderr
            target = int(victim.stdout)
            layout = maf_sandbox.guest_run_layout(
                "process-test/run" if relative else "/tmp/process-test/run"
            )
            physical = maf_sandbox.guest_run_layout("/tmp/process-test/run")
            execute = sandbox.exec
            assert isinstance(sandbox, maf_sandbox.BoundedExec)
            bounded_execute = sandbox.exec_bounded
            scans = 0

            async def check_launcher_close(
                command, *, working_directory, timeout, max_output_bytes=None
            ):
                nonlocal scans
                if " -I -S -c " in str(command) and " --signal " not in str(command):
                    scans += 1
                    if scans == 2:
                        ready = await execute(
                            [
                                "python3",
                                "-c",
                                """import time
from pathlib import Path
for _ in range(100):
    if Path('/tmp/process-witness').exists(): break
    time.sleep(.01)
else: raise RuntimeError('guest did not create its witness')
""",
                            ],
                            working_directory=working_directory,
                            timeout=timeout,
                        )
                        assert ready.exit_code == 0, ready.stderr
                    if scans == 3 and degraded:
                        if degraded == "unavailable":
                            raise PermissionError("process snapshot unavailable")
                        return maf_sandbox.ExecResult(
                            stdout='{"processes": [], "incomplete": true}', exit_code=0
                        )
                if command == f"sh {shlex.quote(layout.launcher)}" or command == (
                    f"sh {transport._quote(transport._layout_path(layout, layout.launcher))}"
                ):
                    check_closed = (
                        "import os; from pathlib import Path; "
                        f"pid=Path({physical.pid!r}).read_text(); "
                        "parent=Path('/proc/'+pid+'/stat').read_text().rsplit(')',1)[1].split()[1]; "
                        "assert os.readlink('/proc/'+parent+'/fd/1') == '/dev/null'"
                    )
                    command += " && python3 -c " + shlex.quote(check_closed)
                if max_output_bytes is not None:
                    return await bounded_execute(
                        command,
                        working_directory=working_directory,
                        timeout=timeout,
                        max_output_bytes=max_output_bytes,
                    )
                return await execute(command, working_directory=working_directory, timeout=timeout)

            monkeypatch.setattr(sandbox, "exec", check_launcher_close)
            monkeypatch.setattr(sandbox, "exec_bounded", check_launcher_close)
            prepared = await sandbox.exec(
                ["mkdir", "-p", layout.work, posixpath.dirname(layout.program)],
                working_directory="/tmp",
                timeout=10,
            )
            assert prepared.exit_code == 0
            program = f"""import json, os, subprocess, time
from pathlib import Path
with open('/proc/' + str(os.getppid()) + '/fd/1', 'w') as control:
    control.write('maf-host-tools: process-v1 {target} {target}\\n')
    control.flush()
child = subprocess.Popen(['sleep', '90'], start_new_session=True)
Path('/tmp/process-witness').write_text(json.dumps(dict(program=os.getpid(), child=child.pid)))
while not Path({physical.pid!r}).exists():
    time.sleep(.01)
Path({physical.pid!r}).write_text({str(target)!r})
Path({physical.session!r}).write_text({str(target)!r})
assert os.getcwd() == {physical.work!r}
print('ready', flush=True)
time.sleep(2 if {finish!r} else 90)
"""
            await sandbox.write_file(layout.program, program, working_directory="/tmp")
            records = Records()
            run = maf_sandbox.HostToolRun(maf_sandbox.HostToolRegistry(observer=records))
            if finish:
                result = await maf_sandbox.host_tool_calls_over_exec(
                    sandbox, run, layout, timeout=8
                )
                assert result.exit_code == 0 and "ready" in result.stdout
            else:
                with pytest.raises(maf_sandbox.SandboxProgramTimeout) as expired:
                    await maf_sandbox.host_tool_calls_over_exec(sandbox, run, layout, timeout=6)
                assert expired.value.reach == "group"
            check = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    f"""import json
from pathlib import Path
w=json.loads(Path('/tmp/process-witness').read_text())
def state(pid):
    try: return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[0]
    except FileNotFoundError: return 'gone'
print(json.dumps(dict(program=state(w['program']),child=state(w['child']),victim=state({target}))))
""",
                ],
                working_directory="/tmp",
                timeout=10,
            )
            assert check.exit_code == 0, check.stderr
            states = json.loads(check.stdout)
            assert states["program"] in {"Z", "gone"}, states
            assert states["child"] in {"Z", "gone"}, states
            assert states["victim"] not in {"Z", "gone"}, states
            assert [s.phase for s in records.snapshots] == [
                "before_launch",
                "after_launch",
                "before_cleanup",
                "after_cleanup",
            ]
            if degraded == "unavailable":
                assert records.snapshots[2].unavailable == "PermissionError"
            else:
                assert all(s.unavailable is None for s in records.snapshots)
            if degraded:
                assert records.snapshots[2].incomplete
                assert not records.snapshots[2].processes
            assert any(p.attribution == "descendant" for p in records.snapshots[1].processes)
            assert any(
                p.uid == 65534 and p.argv and p.attribution == "program"
                for s in records.snapshots
                for p in s.processes
            )
            assert all(s.pid != target and s.pgid != target for s in records.signals)
            assert len(records.signals) == 2
            assert all(s.signal == "SIGKILL" for s in records.signals)
        finally:
            removed = await backend._docker("rm", "-f", name, timeout=30)
            assert removed.returncode == 0, removed.stderr

    asyncio.run(scenario())
