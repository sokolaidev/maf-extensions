"""Public bounded execution captures bytes through bounded HTTP control responses."""

import asyncio
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from maf_sandbox import ExecResult, SandboxExecOutputLimitExceeded, SandboxOutputError

from maf_sandbox_acas._backend import _AcasSandbox


class _CaptureService:
    sandbox_id = "bounded-capture"
    _endpoint = "https://sandbox.example"
    _sbx_path = "/sandboxes/one"
    _api_version = "test"

    def __init__(self, out, err, *, mode="success"):
        self.out, self.err, self.mode = out, err, mode
        self._pipeline = SimpleNamespace(run=self.send)
        self.deleted = False
        self.cleaned = False
        self.closed = 0
        self.calls = 0
        self.started = asyncio.Event()
        self.response_fields = {}
        self.cleanup_fields = {}
        self.cleanup_started = asyncio.Event()
        self.cleanup_failure: str | None = None

    async def begin_delete(self):
        self.deleted = True
        return self

    async def result(self):
        pass

    async def send(self, request, **kwargs):
        self.calls += 1
        script = json.loads(request.content)["command"]
        is_cleanup = not script.startswith("for tool") and "dd if=" not in script
        assert kwargs == {"stream": True, "auto_decompress": False}
        if script.startswith("for tool"):
            self.token = next(
                line[2:].removeprefix("/tmp/")
                for line in script.splitlines()
                if line.startswith("d=")
            )
            text = f"{self.token} 7 {len(self.out)} {len(self.err)}\n"
        elif "dd if=" in script:
            raw = self.out if "dd if=stdout" in script else self.err
            text = self.token + "\n" + base64.b64encode(raw).decode() + "\n" + self.token + "\n"
        else:
            self.cleaned = True
            self.cleanup_started.set()
            if self.cleanup_failure == "raised":
                raise OSError("cleanup transport failed")
            if self.cleanup_failure in {"timeout", "cancel"}:
                await asyncio.Event().wait()
            text = ""
        fields = self.response_fields | (self.cleanup_fields if is_cleanup else {})
        if self.mode == "wire":
            text = "x" * 10000
        service = self

        class Response:
            headers = {}
            status_code = 200

            async def iter_raw(self):
                service.started.set()
                if service.mode in {"timeout", "cancel"}:
                    await asyncio.Event().wait()
                yield json.dumps({"stdout": text, "stderr": "", "exitCode": 0} | fields).encode()

            async def close(self):
                service.closed += 1

        return SimpleNamespace(http_response=Response())

    async def exec(self, command, *, working_directory):
        response = await self.send(
            SimpleNamespace(content=json.dumps({"command": command})),
            stream=True,
            auto_decompress=False,
        )
        raw = b"".join([chunk async for chunk in response.http_response.iter_raw()])
        await response.http_response.close()
        fields = json.loads(raw)
        return SimpleNamespace(
            stdout=fields["stdout"], stderr=fields["stderr"], exit_code=fields["exitCode"]
        )


@pytest.mark.parametrize("field", ["stdout", "stderr", "exitCode"])
def test_malformed_control_response_disposes_and_closes(field):
    async def scenario():
        service = _CaptureService(b"out", b"err")
        service.response_fields[field] = False
        sandbox = _AcasSandbox(service, 1)
        with pytest.raises(ValueError, match="invalid execution response"):
            await sandbox.exec_bounded(
                "program", working_directory="/", timeout=1, max_output_bytes=1024
            )
        assert service.deleted and sandbox._held.unusable
        assert service.closed == service.calls == 1

    asyncio.run(scenario())


def test_bounded_capture_preserves_binary_streams_and_cleans_scratch():
    async def scenario():
        out, err = bytes(range(256)), bytes(range(255, -1, -1))
        service = _CaptureService(out, err)
        result = await _AcasSandbox(service, 1).exec_bounded(
            ["program", "quoted argument"],
            working_directory="/work",
            timeout=1,
            max_output_bytes=1024,
        )
        assert (result.stdout_bytes, result.stderr_bytes, result.exit_code) == (out, err, 7)
        assert service.cleaned and not service.deleted
        assert service.closed == service.calls == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "cleanup_fields",
    [
        {"exitCode": 127, "stderr": "sh: not found"},
        {"exitCode": 1, "stderr": "rm: permission denied"},
        {"stdout": "unexpected cleanup output"},
        {"stderr": "unexpected cleanup diagnostic"},
    ],
    ids=["missing-shell", "permission-denied", "stdout", "stderr"],
)
def test_complete_output_survives_reported_scratch_cleanup_failure(
    bounded, empty, cleanup_fields, caplog
):
    async def scenario():
        out, err = (b"", b"") if empty else (bytes(range(256)), b"\xff\x00error")
        service = _CaptureService(out, err)
        service.cleanup_fields = cleanup_fields
        sandbox = _AcasSandbox(service, 1)
        if bounded:
            result = await sandbox.exec_bounded(
                "program", working_directory="/", timeout=1, max_output_bytes=1024
            )
        else:
            result = await sandbox.exec("program", working_directory="/", timeout=1)
        assert (result.stdout_bytes, result.stderr_bytes, result.exit_code) == (out, err, 7)
        assert service.cleaned and not service.deleted and not sandbox._held.unusable
        assert service.closed == service.calls
        assert "scratch cleanup failed for /tmp/" + service.token in caplog.text
        assert "scratch may remain until sandbox disposal" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("failure", ["raised", "timeout", "cancel"])
def test_interrupted_scratch_cleanup_still_invalidates_and_disposes(bounded, failure):
    async def scenario():
        service = _CaptureService(b"", b"")
        service.cleanup_failure = failure
        sandbox = _AcasSandbox(service, 1)
        if bounded:
            pending = sandbox.exec_bounded(
                "program", working_directory="/", timeout=0.1, max_output_bytes=1024
            )
        else:
            pending = sandbox.exec("program", working_directory="/", timeout=0.1)
        task = asyncio.create_task(pending)
        await service.cleanup_started.wait()
        if failure == "cancel":
            task.cancel()
        expected = {
            "raised": OSError,
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
        }[failure]
        with pytest.raises(expected):
            await task
        assert service.deleted and sandbox._held.unusable

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))


@pytest.mark.parametrize("bounded", [False, True])
def test_result_waits_for_invalidation_on_another_loop(bounded, monkeypatch):
    ready, finish_capture = threading.Event(), threading.Event()
    invalidating, finish_invalidation = threading.Event(), threading.Event()
    result_waiting = threading.Event()
    role = threading.local()
    lock = threading.Lock()

    class Guard:
        def __enter__(self):
            if role.name == "reader" and invalidating.is_set():
                result_waiting.set()
            assert lock.acquire(timeout=5)
            if role.name == "writer":
                invalidating.set()
                assert finish_invalidation.wait(5)

        def __exit__(self, *args):
            lock.release()

    async def capture(*args, **kwargs):
        ready.set()
        assert finish_capture.wait(5)
        return ExecResult(stdout_bytes=b"ok", exit_code=0)

    monkeypatch.setattr(f"{_AcasSandbox.__module__}.capture", capture)
    service = _CaptureService(b"", b"")
    reader = _AcasSandbox(service, 10)
    writer = _AcasSandbox(service, 10, held=reader._held)
    monkeypatch.setattr(reader._held, "invalidation_guard", Guard())

    def read():
        role.name = "reader"
        command = (
            reader.exec_bounded("program", working_directory="/", timeout=10, max_output_bytes=50)
            if bounded
            else reader.exec("program", working_directory="/", timeout=10)
        )
        return asyncio.run(command)

    def invalidate():
        role.name = "writer"
        asyncio.run(writer._invalidate_after_exec(SandboxOutputError("other exec failed")))

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(read)
        assert ready.wait(5)
        cleanup = pool.submit(invalidate)
        try:
            assert invalidating.wait(5)
            finish_capture.set()
            assert result_waiting.wait(3)
            assert not result.done()
        finally:
            finish_capture.set()
            finish_invalidation.set()
        with pytest.raises(SandboxOutputError, match="concurrent exec failure"):
            result.result(timeout=5)
        assert cleanup.result(timeout=5) is None
    assert service.deleted


@pytest.mark.parametrize("second_outcome", ["success", "failure", "cancel"])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_concurrent_handles_share_invalidation_cleanup(second_outcome, delete_fails, monkeypatch):
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        second_leaving = asyncio.Event()
        deleting, finish_delete = asyncio.Event(), asyncio.Event()
        delete_calls = 0

        async def capture(command, *args, **kwargs):
            index = int(command)
            started[index].set()
            try:
                await release[index].wait()
                if index == 0 or second_outcome == "failure":
                    raise SandboxOutputError("capture failed")
                return ExecResult(stdout_bytes=b"ok", exit_code=0)
            finally:
                if index == 1:
                    second_leaving.set()

        service = _CaptureService(b"", b"")

        async def delete():
            nonlocal delete_calls
            delete_calls += 1
            deleting.set()
            await finish_delete.wait()
            if delete_fails:
                raise RuntimeError("delete unavailable")
            return service

        monkeypatch.setattr(f"{_AcasSandbox.__module__}.capture", capture)
        monkeypatch.setattr(service, "begin_delete", delete)
        first = _AcasSandbox(service, 10)
        second = _AcasSandbox(service, 10, held=first._held)
        tasks = [
            asyncio.create_task(first.exec("0", working_directory="/", timeout=10)),
            asyncio.create_task(
                second.exec_bounded("1", working_directory="/", timeout=10, max_output_bytes=50)
            ),
        ]
        await asyncio.gather(*(event.wait() for event in started))
        release[0].set()
        await deleting.wait()
        if second_outcome == "cancel":
            tasks[1].cancel()
        else:
            release[1].set()
        await second_leaving.wait()
        assert not any(task.done() for task in tasks)
        assert delete_calls == 1
        finish_delete.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], SandboxOutputError)
        expected = asyncio.CancelledError if second_outcome == "cancel" else SandboxOutputError
        assert isinstance(results[1], expected)
        assert delete_calls == 1
        for result in results:
            if not isinstance(result, asyncio.CancelledError):
                assert bool(getattr(result, "__notes__", [])) is delete_fails

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["combined", "wire", "timeout", "cancel"])
def test_bounded_failure_disposes_and_refuses_reuse(mode):
    async def scenario():
        service = _CaptureService(b"x" * 150, b"y" * 150, mode=mode)
        sandbox = _AcasSandbox(service, 1)
        task = asyncio.create_task(
            sandbox.exec_bounded(
                "program",
                working_directory="/work",
                timeout=0.02 if mode == "timeout" else 1,
                max_output_bytes=250,
            )
        )
        await service.started.wait()
        if mode == "cancel":
            task.cancel()
        expected = {"timeout": TimeoutError, "cancel": asyncio.CancelledError}.get(
            mode, SandboxExecOutputLimitExceeded
        )
        with pytest.raises(expected):
            await task
        assert service.deleted and service.closed == 1 and sandbox._held.unusable
        assert service.calls == 1
        with pytest.raises(SandboxOutputError, match="invalidated"):
            await sandbox.exec_bounded(
                "true", working_directory="/", timeout=1, max_output_bytes=250
            )

    asyncio.run(scenario())
