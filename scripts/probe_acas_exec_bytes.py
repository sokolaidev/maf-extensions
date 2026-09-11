"""Measure a guest-side base64 exec envelope against one disposable ACAS sandbox.

Run with the workspace environment and the ACAS_SANDBOX_* settings used by live tests.
This is a feasibility probe, not an alternative backend implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.core.rest import HttpRequest
from maf_sandbox import Capability, ExecResult, SandboxKey, SandboxSpec
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

_BEGIN = "maf-exec-bytes-v1:begin"
_STDERR = "maf-exec-bytes-v1:stderr"
_END = "maf-exec-bytes-v1:end"


@dataclass(frozen=True)
class Captured:
    """The streams decoded from a complete experimental envelope."""

    stdout: bytes
    stderr: bytes
    exit_code: int


def wrap_command(command: str | list[str], *, helper: str = "base64") -> str:
    """Capture each stream in a private temporary directory before the service sees it."""
    cmd = command if isinstance(command, str) else shlex.join(command)
    encoder = shlex.quote(helper)
    return f"""for tool in sh mktemp {encoder} rm rmdir; do
    command -v "$tool" >/dev/null 2>&1 || exit 127
done
d=$(mktemp -d /tmp/maf-exec-bytes-XXXXXXXXXX) || exit 125
trap 'rm -f "$d/stdout" "$d/stderr"; rmdir "$d"' 0
sh -c {shlex.quote(cmd)} > "$d/stdout" 2> "$d/stderr"
rc=$?
printf '%s\\n%s\\n' '{_BEGIN}' "$rc"
{encoder} "$d/stdout" || exit 125
printf '\\n%s\\n' '{_STDERR}'
{encoder} "$d/stderr" || exit 125
printf '\\n%s\\n' '{_END}'
exit "$rc"
"""


def decode_envelope(stdout: str, stderr: str, exit_code: int) -> Captured:
    """Reject incomplete framing and invalid base64 rather than reporting partial success."""
    lines = stdout.splitlines()
    if (
        stderr
        or len(lines) < 5
        or lines[0] != _BEGIN
        or lines[-1] != _END
        or not stdout.endswith(_END + "\n")
    ):
        raise ValueError("missing envelope or unexpected transport diagnostics")
    status = int(lines[1])
    if status != exit_code or not 0 <= status <= 255:
        raise ValueError("exit status disagrees with the envelope")
    if lines.count(_STDERR) != 1:
        raise ValueError("missing or repeated stream separator")
    separator = lines.index(_STDERR)
    return Captured(
        stdout=base64.b64decode("".join(lines[2:separator]), validate=True),
        stderr=base64.b64decode("".join(lines[separator + 1 : -1]), validate=True),
        exit_code=status,
    )


def file_capture_command(command: str | list[str], directory: str) -> str:
    """Drain separate pipes to files, waiting for inherited writers as ordinary exec does."""
    cmd = command if isinstance(command, str) else shlex.join(command)
    paths = {
        name: shlex.quote(f"{directory}/{name}")
        for name in ("outpipe", "errpipe", "stdout", "stderr")
    }
    return f"""for tool in sh mkdir mkfifo cat; do
    command -v "$tool" >/dev/null 2>&1 || exit 127
done
mkdir -m 700 {shlex.quote(directory)} || exit 125
mkfifo {paths["outpipe"]} {paths["errpipe"]} || exit 125
cat {paths["outpipe"]} > {paths["stdout"]} & out_reader=$!
cat {paths["errpipe"]} > {paths["stderr"]} & err_reader=$!
sh -c {shlex.quote(cmd)} > {paths["outpipe"]} 2> {paths["errpipe"]}
rc=$?
wait "$out_reader" || exit 125
wait "$err_reader" || exit 125
exit "$rc"
"""


def _program(stdout: bytes, stderr: bytes, status: int = 7) -> list[str]:
    return [
        "python3",
        "-c",
        "import base64,sys; "
        f"sys.stdout.buffer.write(base64.b64decode({base64.b64encode(stdout).decode()!r})); "
        f"sys.stderr.buffer.write(base64.b64decode({base64.b64encode(stderr).decode()!r})); "
        f"sys.exit({status})",
    ]


def _summary(actual: bytes, expected: bytes) -> dict[str, Any]:
    return {
        "expected_bytes": len(expected),
        "received_bytes": len(actual),
        "sha256": hashlib.sha256(actual).hexdigest(),
        "exact": actual == expected,
    }


def _case_result(result: ExecResult, out: bytes, err: bytes, status: int) -> dict[str, Any]:
    """Record invalid envelopes as failed measurements, retaining available diagnostics."""
    try:
        captured = decode_envelope(result.stdout, result.stderr, result.exit_code)
        return {
            "stdout": _summary(captured.stdout, out),
            "stderr": _summary(captured.stderr, err),
            "exit_code": captured.exit_code,
            "passed": captured.stdout == out
            and captured.stderr == err
            and captured.exit_code == status,
        }
    except (ValueError, UnicodeError) as exc:
        lines = result.stdout.splitlines()
        return {
            "passed": False,
            "error": type(exc).__name__,
            "received_text_characters": len(result.stdout),
            "transport_stderr": result.stderr,
            "framed_exit_code": lines[1] if len(lines) > 1 and lines[0] == _BEGIN else None,
            "exit_code": result.exit_code,
        }


def _count_capture_directories(result: ExecResult) -> int:
    """Count scratch only after a successful, diagnostic-free inventory."""
    if result.exit_code != 0 or result.stderr:
        raise ValueError("capture directory inventory failed or returned diagnostics")
    return len(result.stdout.splitlines())


async def measure(output: Path) -> dict[str, Any]:
    """Create one sandbox, record measurements without resource identifiers, and dispose it."""
    config = AcasSandboxConfig(
        endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
        subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
        sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
        read_timeout_seconds=30,
    )
    backend = AcasSandboxBackend(config)
    key = SandboxKey("exec-bytes-" + uuid.uuid4().hex[:12], "probe", "research")
    report: dict[str, Any] = {"image": "python-3.13", "cases": []}

    def save() -> None:
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    try:
        print("Acquiring one disposable sandbox", flush=True)
        sandbox = await backend.acquire(
            key,
            SandboxSpec(
                kind="exec-bytes",
                image="python-3.13",
                work_dir="/tmp/exec-bytes-work",
                requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            ),
        )
        sc = sandbox._sc
        work = "/tmp/exec-bytes-work"
        # Inspect the actual HTTP payload before the SDK builds its typed ExecResult.
        request = HttpRequest(
            "POST",
            f"{sc._endpoint}{sc._sbx_path}/executeShellCommand",
            json={
                "command": shlex.join(_program(b"ok\xff\xfe", b"err\xff\xfe")),
                "workingDirectory": work,
            },
            params={"api-version": sc._api_version},
        )
        response = await sc._send_request(request)
        response.raise_for_status()
        raw = response.json()
        report["service_baseline"] = {
            "stdout_codepoints": [hex(ord(c)) for c in raw["stdout"]],
            "stderr_codepoints": [hex(ord(c)) for c in raw["stderr"]],
            "exit_code": raw["exitCode"],
            "http_payload_has_replacement_utf8": b"\xef\xbf\xbd" in response.content,
        }
        save()

        async def run_case(
            name: str, command: str | list[str], out: bytes, err: bytes, status: int = 7
        ) -> None:
            started = time.monotonic()
            result = await sandbox._exec_text(
                wrap_command(command), working_directory=work, timeout=60
            )
            case: dict[str, Any] = {"name": name, "seconds": round(time.monotonic() - started, 3)}
            case.update(_case_result(result, out, err, status))
            report["cases"].append(case)
            print(name + ": " + ("exact" if case["passed"] else "FAILED"), flush=True)
            save()

        corpus = bytes(range(256)) + "\r\nvalid \ufffd \u2713\n".encode() + b"\xc3\xe2\x82\xff\xfe"
        await run_case("binary-argv", _program(corpus, corpus[::-1]), corpus, corpus[::-1])
        await run_case(
            "binary-shell", shlex.join(_program(corpus, corpus[::-1])), corpus, corpus[::-1]
        )
        await run_case("empty", ["sh", "-c", "exit 0"], b"", b"", 0)
        quoted = "quotes ' \" $() ; & |\nsecond line"
        await run_case(
            "quoted-argv",
            ["python3", "-c", "import sys; sys.stdout.write(sys.argv[1])", quoted],
            quoted.encode(),
            b"",
            0,
        )
        for size in (65536, 1048576):
            payload = bytes(range(256)) * (size // 256)
            command = [
                "python3",
                "-c",
                f"import sys; p=bytes(range(256))*{size // 256}; sys.stdout.buffer.write(p); sys.stderr.buffer.write(p); sys.exit(7)",
            ]
            await run_case(f"large-{size}-each-stream", command, payload, payload)
        await asyncio.gather(
            *(
                run_case(
                    f"concurrent-{i}",
                    _program(bytes([128 + i]) * 1024, bytes([240 + i]) * 1024),
                    bytes([128 + i]) * 1024,
                    bytes([240 + i]) * 1024,
                )
                for i in range(3)
            )
        )
        print("Measuring capture through the binary file API", flush=True)
        directory = f"{work}/files-{uuid.uuid4().hex}"
        paths = [f"{directory}/stdout", f"{directory}/stderr"]
        payload = bytes(range(256)) * 4096
        command = [
            "python3",
            "-c",
            "import sys; p=bytes(range(256))*4096; sys.stdout.buffer.write(p); sys.stderr.buffer.write(p); sys.exit(7)",
        ]
        try:
            result = await sandbox._exec_text(
                f"mkdir -m 700 {shlex.quote(directory)} && sh -c {shlex.quote(shlex.join(command))} > {shlex.quote(paths[0])} 2> {shlex.quote(paths[1])}",
                working_directory=work,
                timeout=30,
            )
            captured_files = [
                await sandbox.read_file(path, working_directory=work, max_bytes=len(payload) + 1)
                for path in paths
            ]
            report["file_capture"] = {
                "stdout": _summary(captured_files[0], payload),
                "stderr": _summary(captured_files[1], payload),
                "exit_code": result.exit_code,
            }
        finally:
            removed = await sandbox._exec_text(
                f"rm -f {shlex.join(paths)} && rmdir {shlex.quote(directory)}",
                working_directory=work,
                timeout=20,
            )
            report["file_capture_cleanup_succeeded"] = removed.exit_code == 0
        background = "printf before; (sleep 2; printf after) &"
        direct = await sandbox._exec_text(background, working_directory=work, timeout=20)
        wrapped = await sandbox._exec_text(
            wrap_command(background), working_directory=work, timeout=20
        )
        try:
            captured = decode_envelope(wrapped.stdout, wrapped.stderr, wrapped.exit_code)
            report["background_writer"] = {
                "direct": direct.stdout,
                "wrapped": captured.stdout.decode("ascii"),
            }
        except ValueError:
            report["background_writer"] = {"direct": direct.stdout, "wrapped": "invalid envelope"}
        report["pipe_file_capture"] = []

        async def pipe_case(
            name: str, command: str | list[str], out: bytes, err: bytes, status: int
        ) -> None:
            directory = f"{work}/pipes-{uuid.uuid4().hex}"
            try:
                result = await sandbox._exec_text(
                    file_capture_command(command, directory), working_directory=work, timeout=30
                )
                streams = [
                    await sandbox.read_file(
                        f"{directory}/{name}",
                        working_directory=work,
                        max_bytes=max(len(out), len(err)) + 1,
                    )
                    for name in ("stdout", "stderr")
                ]
                report["pipe_file_capture"].append(
                    {
                        "name": name,
                        "stdout": _summary(streams[0], out),
                        "stderr": _summary(streams[1], err),
                        "exit_code": result.exit_code,
                        "passed": streams == [out, err] and result.exit_code == status,
                    }
                )
                print(name + ": pipe/file capture measured", flush=True)
            finally:
                removed = await sandbox._exec_text(
                    "rm -f "
                    + shlex.join(
                        [
                            f"{directory}/{name}"
                            for name in ("stdout", "stderr", "outpipe", "errpipe")
                        ]
                    )
                    + f" && rmdir {shlex.quote(directory)}",
                    working_directory=work,
                    timeout=20,
                )
                if removed.exit_code:
                    raise RuntimeError("pipe/file capture cleanup failed")

        await pipe_case("background-writer", background, b"beforeafter", b"", 0)
        await asyncio.gather(
            *(pipe_case(f"large-concurrent-{i}", command, payload, payload, 7) for i in range(2))
        )
        save()
        missing = await sandbox._exec_text(
            wrap_command("touch should-not-exist", helper="maf-no-such-encoder"),
            working_directory=work,
            timeout=20,
        )
        marker = await sandbox._exec_text(
            "test ! -e should-not-exist", working_directory=work, timeout=20
        )
        report["missing_helper_refuses_before_program"] = (
            missing.exit_code == 127 and marker.exit_code == 0
        )
        # A client-side timeout/cancellation does not send a signal to the remote shell.
        for cancel in (False, True):
            stamp = "cancel-completed" if cancel else "timeout-completed"
            command = f"sleep 6; printf finished > {stamp}"
            task = asyncio.create_task(
                sandbox._exec_text(
                    wrap_command(command), working_directory=work, timeout=1 if not cancel else 30
                )
            )
            if cancel:
                await asyncio.sleep(1)
                task.cancel()
            try:
                await task
                outcome = "returned"
            except TimeoutError:
                outcome = "TimeoutError"
            except asyncio.CancelledError:
                outcome = "CancelledError"
            await asyncio.sleep(7)
            marker = await sandbox._exec_text(
                f"test -f {stamp}", working_directory=work, timeout=20
            )
            report["cancellation" if cancel else "timeout"] = {
                "host_outcome": outcome,
                "guest_continued": marker.exit_code == 0,
            }
            save()
        leftovers = await sandbox._exec_text(
            "find /tmp -maxdepth 1 -type d -name 'maf-exec-bytes-*' -print",
            working_directory=work,
            timeout=20,
        )
        report["capture_directories_remaining"] = _count_capture_directories(leftovers)
    finally:
        try:
            print("Disposing the probe scope", flush=True)
            purged = await backend.dispose_scope(key.scope, key.thread_id)
            report["cleanup_refused"] = purged.undisposed is not None
            for attempt in range(10):
                remaining = [
                    item
                    async for item in backend._group_client().list_sandboxes(
                        labels={"scope": key.scope, "thread": key.thread_id}
                    )
                ]
                if not remaining:
                    report["scope_confirmed_empty"] = True
                    break
                await asyncio.sleep(3)
            else:
                report["scope_confirmed_empty"] = False
        finally:
            save()
            await backend.aclose()
    return report


def main() -> int:
    """Run the opt-in probe; never create resources merely by importing this module."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Create and dispose one billable ACAS sandbox",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(measure(args.output))
    print(json.dumps(report, indent=2, ensure_ascii=True))
    passed = (
        report.get("scope_confirmed_empty")
        and not report.get("cleanup_refused")
        and report.get("file_capture_cleanup_succeeded")
        and report.get("missing_helper_refuses_before_program")
        and report.get("capture_directories_remaining") == 0
        and all(case["passed"] for case in report["cases"])
        and all(case["passed"] for case in report["pipe_file_capture"])
        and report["file_capture"]["stdout"]["exact"]
        and report["file_capture"]["stderr"]["exact"]
        and report["file_capture"]["exit_code"] == 7
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
