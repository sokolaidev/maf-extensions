"""Streamed ACAS responses are capped before parsing, including HTTP failures."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest
from azure.core.exceptions import HttpResponseError
from maf_sandbox import SandboxExecOutputLimitExceeded

from maf_sandbox_acas._backend import _AcasSandbox


@pytest.mark.parametrize("stream", ["stdout", "stderr", "error"])
def test_cap_is_enforced_before_decoding_and_response_is_closed(stream):
    async def scenario():
        pulled = 0

        class Response:
            headers = {}
            status_code = 500 if stream == "error" else 200
            closed = False

            async def iter_raw(self):
                nonlocal pulled
                for index in range(1000):
                    pulled += 1
                    prefix = f'{{"{stream}":"'.encode() if index == 0 else b""
                    yield prefix + b"x" * (100 - len(prefix))

            async def close(self):
                self.closed = True

        response = Response()

        async def send(request, **kwargs):
            assert kwargs == {"stream": True, "auto_decompress": False}
            assert request.headers["Accept-Encoding"] == "identity"
            assert json.loads(request.content)["workingDirectory"] == "/maf-sandbox/work/child"
            return SimpleNamespace(http_response=response)

        client = SimpleNamespace(
            _endpoint="https://sandbox.example",
            _sbx_path="/sandboxes/one",
            _api_version="test",
            _pipeline=SimpleNamespace(run=send),
        )
        sandbox = _AcasSandbox(None, 1)
        sandbox._sc = client
        with pytest.raises(SandboxExecOutputLimitExceeded):
            await sandbox.exec_bounded(
                "probe", working_directory="child", timeout=1, max_output_bytes=250
            )
        assert pulled == 3 and response.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 500])
def test_small_complete_response_preserves_result_or_http_failure(status):
    async def scenario():
        class Response:
            headers = {}
            status_code = status
            closed = False

            async def iter_raw(self):
                yield json.dumps({"stdout": "out", "stderr": "err", "exitCode": 7}).encode()

            async def close(self):
                self.closed = True

        response = Response()

        async def send(request, **kwargs):
            return SimpleNamespace(http_response=response)

        sandbox = _AcasSandbox(None, 1)
        sandbox._sc = SimpleNamespace(
            _endpoint="https://sandbox.example",
            _sbx_path="/sandboxes/one",
            _api_version="test",
            _pipeline=SimpleNamespace(run=send),
        )
        if status == 500:
            with pytest.raises(HttpResponseError):
                await sandbox.exec_bounded(
                    "probe", working_directory="/work", timeout=1, max_output_bytes=250
                )
        else:
            result = await sandbox.exec_bounded(
                "probe", working_directory="/work", timeout=1, max_output_bytes=250
            )
            assert (result.stdout, result.stderr, result.exit_code) == ("out", "err", 7)
        assert response.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("channel", ["stdout", "stderr", "error"])
def test_sdk_pipeline_does_not_buffer_the_http_body(channel, monkeypatch):
    from azure.containerapps.sandbox.aio import SandboxClient
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.pipeline import AsyncPipeline
    from azure.core.pipeline.transport import AioHttpTransport
    from azure.core.rest._aiohttp import RestAioHttpTransportResponse

    async def forbidden_read(self):
        pytest.fail("the SDK buffered the complete response")

    monkeypatch.setattr(RestAioHttpTransportResponse, "read", forbidden_read)

    async def scenario():
        finished = asyncio.Event()

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                status = b"500 Error" if channel == "error" else b"200 OK"
                writer.write(
                    b"HTTP/1.1 "
                    + status
                    + b"\r\nContent-Length: 8388608\r\nConnection: close\r\n\r\n"
                )
                writer.write(f'{{"{channel}":"'.encode())
                for _ in range(1024):
                    writer.write(b"x" * 8192)
                    await writer.drain()
                    await asyncio.sleep(0.001)
            except (ConnectionError, asyncio.IncompleteReadError):
                # The bounded reader closes the connection before the producer finishes.
                pass
            finally:
                writer.close()
                finished.set()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server, AioHttpTransport() as transport:
            pipeline = AsyncPipeline(transport=transport, policies=[])
            client = SandboxClient(
                "https://sandbox.example",
                cast(AsyncTokenCredential, object()),
                subscription_id="test",
                resource_group="test",
                sandbox_group="test",
                sandbox_id="test",
                _pipeline=pipeline,
            )
            client._endpoint = f"http://127.0.0.1:{port}"
            sandbox = _AcasSandbox(client, read_timeout=1)
            with pytest.raises(SandboxExecOutputLimitExceeded):
                await sandbox.exec_bounded(
                    "probe", working_directory="/work", timeout=5, max_output_bytes=1024
                )
            await asyncio.wait_for(finished.wait(), timeout=5)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel", "compressed"])
def test_response_closes_when_collection_cannot_complete(mode):
    async def scenario():
        entered = asyncio.Event()

        class Response:
            headers = {"Content-Encoding": "gzip"} if mode == "compressed" else {}
            status_code = 200
            closed = False

            async def iter_raw(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def close(self):
                self.closed = True

        response = Response()

        async def send(request, **kwargs):
            return SimpleNamespace(http_response=response)

        client = SimpleNamespace(
            _endpoint="https://sandbox.example",
            _sbx_path="/sandboxes/one",
            _api_version="test",
            _pipeline=SimpleNamespace(run=send),
        )
        task = asyncio.create_task(
            _AcasSandbox(client, 1).exec_bounded(
                "probe",
                working_directory="/work",
                timeout=0.05 if mode == "timeout" else 2,
                max_output_bytes=1024,
            )
        )
        expected = ValueError if mode == "compressed" else TimeoutError
        if mode == "cancel":
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            expected = asyncio.CancelledError
        with pytest.raises(expected):
            await task
        assert response.closed

    asyncio.run(scenario())
