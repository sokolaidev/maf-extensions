# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2"]
# ///
"""Characterize the installed OpenShell SDK against a local gRPC fixture for #1797."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path


def main() -> None:
    """Emit SDK observations; the fixture does not execute guest programs."""
    grpc = import_module("grpc")
    sdk = import_module("openshell.sandbox")
    pb = import_module("openshell._proto.openshell_pb2")
    version = importlib.metadata.version("openshell")
    if version != "0.1.2":
        raise RuntimeError(f"expected openshell 0.1.2, got {version}")
    requests = []
    raw_stdout = b"\x00\xff\xfe\x80"
    raw_stderr = b"\xfe\x00\xff"

    def execute(request, context):
        requests.append(request)
        if request.command == ["bulk"]:
            for _ in range(64):
                yield pb.ExecSandboxEvent(stdout=pb.ExecSandboxStdout(data=b"x" * 131072))
        else:
            yield pb.ExecSandboxEvent(stdout=pb.ExecSandboxStdout(data=raw_stdout))
            yield pb.ExecSandboxEvent(stderr=pb.ExecSandboxStderr(data=raw_stderr))
        yield pb.ExecSandboxEvent(exit=pb.ExecSandboxExit(exit_code=124))

    def delete(request, context):
        requests.append(request)
        return pb.DeleteSandboxResponse(outcome=1, sandbox_id="fixture-instance-b")

    service = pb.DESCRIPTOR.services_by_name["OpenShell"].full_name
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    service,
                    {
                        "ExecSandbox": grpc.unary_stream_rpc_method_handler(
                            execute,
                            request_deserializer=pb.ExecSandboxRequest.FromString,
                            response_serializer=pb.ExecSandboxEvent.SerializeToString,
                        ),
                        "DeleteSandbox": grpc.unary_unary_rpc_method_handler(
                            delete,
                            request_deserializer=pb.DeleteSandboxRequest.FromString,
                            response_serializer=pb.DeleteSandboxResponse.SerializeToString,
                        ),
                    },
                ),
            )
        )
        port = server.add_insecure_port("127.0.0.1:0")
        if not port:
            raise RuntimeError("could not bind loopback fixture")
        server.start()
        try:
            with sdk.SandboxClient(f"127.0.0.1:{port}") as client:
                result = client.exec("fixture", ["bytes"], workspace="default")
                chunks = list(client.exec_stream("fixture", ["bytes"], workspace="default"))
                stdout = b"".join(
                    item.data
                    for item in chunks
                    if isinstance(item, sdk.ExecChunk) and item.stream == "stdout"
                )
                stderr = b"".join(
                    item.data
                    for item in chunks
                    if isinstance(item, sdk.ExecChunk) and item.stream == "stderr"
                )
                if not (stdout == raw_stdout and stderr == raw_stderr):
                    raise RuntimeError(
                        "probe expectation failed: stdout == raw_stdout and stderr == raw_stderr"
                    )
                if not (result.stdout.encode() != raw_stdout):
                    raise RuntimeError(
                        "probe expectation failed: result.stdout.encode() != raw_stdout"
                    )
                if not (result.stderr.encode() != raw_stderr):
                    raise RuntimeError(
                        "probe expectation failed: result.stderr.encode() != raw_stderr"
                    )
                before_fractional = len(requests)
                try:
                    client.exec("fixture", ["bytes"], workspace="default", timeout_seconds=0.25)
                except TypeError as exc:
                    fractional_error = type(exc).__name__
                else:
                    raise AssertionError("fractional timeout unexpectedly accepted")
                if not (len(requests) == before_fractional):
                    raise RuntimeError(
                        "probe expectation failed: len(requests) == before_fractional"
                    )

                tracemalloc.start()
                total = 0
                retained_after_last_chunk = 0
                for item in client.exec_stream("fixture", ["bulk"], workspace="default"):
                    if isinstance(item, sdk.ExecChunk):
                        total += len(item.data)
                        retained_after_last_chunk = tracemalloc.get_traced_memory()[0]
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                if not (total == 8388608):
                    raise RuntimeError("probe expectation failed: total == 8388608")
                deletion = client.delete("fixture", workspace="default")
                if not (deletion.sandbox_id == "fixture-instance-b"):
                    raise RuntimeError(
                        'probe expectation failed: deletion.sandbox_id == "fixture-instance-b"'
                    )
        finally:
            server.stop(0).wait()

    observations = {
        "scope": "installed SDK with loopback gRPC fixture; no OpenShell gateway or guest",
        "openshell_version": version,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "sdk_source_sha256": hashlib.sha256(Path(str(sdk.__file__)).read_bytes()).hexdigest(),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("grpcio", "protobuf", "cloudpickle", "httpx", "googleapis-common-protos")
        },
        "raw_stdout_hex": stdout.hex(),
        "raw_stderr_hex": stderr.hex(),
        "convenience_stdout_reencoded_hex": result.stdout.encode().hex(),
        "convenience_stderr_reencoded_hex": result.stderr.encode().hex(),
        "exit_code_124_returned_normally": result.exit_code == 124,
        "fractional_timeout_error_before_rpc": fractional_error,
        "stream_bytes_consumed_without_caller_accumulation": total,
        "traced_bytes_retained_after_last_raw_chunk": retained_after_last_chunk,
        "traced_peak_bytes": peak,
        "delete_request_fields": sorted(pb.DeleteSandboxRequest.DESCRIPTOR.fields_by_name),
        "exec_exit_fields": sorted(pb.ExecSandboxExit.DESCRIPTOR.fields_by_name),
        "sdk_request_ids": [request.request_id for request in requests],
        "fixture_delete_target": deletion.sandbox_id,
    }
    print(json.dumps(observations, indent=2))


if __name__ == "__main__":
    main()
