"""Public bounded execution captures bytes through bounded HTTP control responses."""

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from maf_sandbox import SandboxExecOutputLimitExceeded, SandboxOutputError

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

    async def begin_delete(self):
        self.deleted = True
        return self

    async def result(self):
        pass

    async def send(self, request, **kwargs):
        self.calls += 1
        script = json.loads(request.content)["command"]
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
            text = ""
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
                yield json.dumps({"stdout": text, "stderr": "", "exitCode": 0}).encode()

            async def close(self):
                service.closed += 1

        return SimpleNamespace(http_response=Response())


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
