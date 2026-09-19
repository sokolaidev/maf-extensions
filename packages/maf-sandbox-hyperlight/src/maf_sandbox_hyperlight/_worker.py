"""Run the pinned native SDK on one thread, behind the parent process's deadline."""

from __future__ import annotations

import ctypes
import importlib
import os
import sys
from collections.abc import Callable
from importlib.metadata import version
from typing import Protocol, cast

from ._config import MAX_CODE_BYTES, MAX_OUTPUT_BYTES
from ._wire import decode, encode


class _NativeResult(Protocol):
    stdout: str
    stderr: str
    exit_code: int


class _NativeSandbox(Protocol):
    def run(self, code: str) -> _NativeResult: ...
    def snapshot(self) -> object: ...
    def restore(self, snapshot: object) -> None: ...
    def allow_domain(self, target: str) -> None: ...


def main() -> None:
    """Initialize only after the parent has assigned this process to its lifetime job."""
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0) as channel:
        # Native diagnostics must not be mistaken for protocol messages.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        request = decode(sys.stdin.buffer.readline(6 * MAX_CODE_BYTES + 4096))
        try:
            if request.get("op") != "init":
                raise ValueError("first message must initialize the worker")
            for package in (
                "hyperlight-sandbox",
                "hyperlight-sandbox-backend-wasm",
                "hyperlight-sandbox-python-guest",
            ):
                if version(package) != "0.7.0":
                    raise RuntimeError(f"{package} must be exactly 0.7.0")
            if sys.platform == "linux":
                from ._linux import check_kvm

                check_kvm()
            else:
                _hypervisor_library = ctypes.WinDLL("WinHvPlatform.dll")
            sdk = importlib.import_module("hyperlight_sandbox")
            constructor = cast("Callable[..., _NativeSandbox]", sdk.Sandbox)
            file_options: dict[str, object] = {}
            if "output_dir" in request:
                directory = request["output_dir"]
                if not isinstance(directory, str):
                    raise ValueError("invalid output directory")
                file_options = {
                    "output_dir": directory,
                    "max_file_size": "8Mi",
                    "max_total_size": "32Mi",
                    "max_file_count": 64,
                }
            sandbox = constructor(
                backend="wasm",
                module="python_guest.path",
                heap_size="400Mi",
                stack_size="200Mi",
                **file_options,
            )
            targets = request["targets"]
            if not isinstance(targets, list):
                raise ValueError("targets must be a list")
            for target in cast("list[object]", targets):
                if not isinstance(target, str):
                    raise ValueError("targets must be strings")
                sandbox.allow_domain(target)
            output_limit = request["output_limit"]
            if type(output_limit) is not int or not 0 < output_limit <= MAX_OUTPUT_BYTES:
                raise ValueError("invalid output limit")
            warm = sandbox.run("pass")
            if warm.exit_code != 0:
                raise RuntimeError("the packaged Python guest failed initialization")
            baseline = sandbox.snapshot()
            channel.write(encode({"ok": True}))
            while raw := sys.stdin.buffer.readline(6 * MAX_CODE_BYTES + 4096):
                request = decode(raw)
                if request.get("op") == "reset":
                    sandbox.restore(baseline)
                    response: dict[str, object] = {"ok": True}
                elif request.get("op") == "run":
                    code = request["code"]
                    if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_CODE_BYTES:
                        raise ValueError("invalid program text")
                    result = sandbox.run(code)
                    if len(result.stdout.encode()) + len(result.stderr.encode()) > output_limit:
                        response = {"error": "output_limit"}
                    else:
                        response = {
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": result.exit_code,
                        }
                else:
                    raise ValueError("unknown worker operation")
                channel.write(encode(response))
        except BaseException as error:
            channel.write(encode({"error": "native", "detail": str(error)[:4096]}))


if __name__ == "__main__":
    main()
