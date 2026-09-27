"""Raw wasi-http requests retain policy errors separately from transport failures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from maf_sandbox import Capability, Sandbox

RAW_REQUEST = """
import json
import wit_world
from wit_world.imports import wasi_http_types as t, outgoing_handler as oh

def request_once(method, scheme, authority, path, payload, content_length):
    standard = {"GET": t.Method_Get, "HEAD": t.Method_Head,
                "POST": t.Method_Post, "PUT": t.Method_Put,
                "PATCH": t.Method_Patch, "DELETE": t.Method_Delete,
                "OPTIONS": t.Method_Options, "TRACE": t.Method_Trace,
                "CONNECT": t.Method_Connect}
    fields = t.Fields()
    if content_length:
        fields.set("content-length", [str(len(payload)).encode("ascii")])
    request = t.OutgoingRequest(fields)
    request.set_method(standard[method]() if method in standard else t.Method_Other(method))
    request.set_scheme(t.Scheme_Https() if scheme == "https" else t.Scheme_Http())
    request.set_authority(authority)
    request.set_path_with_query(path)
    body = request.body()
    try:
        if payload:
            stream = body.write()
            stream.blocking_write_and_flush(payload)
            del stream
        t.OutgoingBody.finish(body, None)
        response = oh.handle(request, None)
        response.subscribe().block()
        result = response.get()
        while not hasattr(result, "status"):
            result = result.value
        return {"status": result.status()}
    except wit_world.Err as error:
        return {"error": repr(error.value)}
"""


@dataclass(frozen=True)
class RawHttpSubject:
    sandbox: Sandbox
    capabilities: frozenset[Capability]

    async def request(
        self,
        method: str,
        url: str,
        *,
        payload: bytes = b"",
        content_length: bool = False,
        timeout: float = 15,
    ) -> dict[str, object]:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("the request harness requires HTTP or HTTPS")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        arguments = (str(method), parts.scheme, parts.netloc, path, payload, content_length)
        result = await self.sandbox.run_code(
            RAW_REQUEST + f"\nprint(json.dumps(request_once(*{arguments!r})))", timeout=timeout
        )
        if result.exit_code != 0:
            raise AssertionError(f"raw wasi-http harness failed: {result.stderr}")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or set(response) not in ({"status"}, {"error"}):
            raise AssertionError(f"unexpected raw wasi-http result: {response!r}")
        return response

    async def http_reaches(self, method: str, url: str, *, timeout: float) -> bool:
        response = await self.request(method, url, timeout=timeout)
        if "error" in response:
            if "HttpRequestDenied" not in str(response["error"]):
                raise AssertionError(f"request failed without policy denial: {response}")
            return False
        return response["status"] == 200
