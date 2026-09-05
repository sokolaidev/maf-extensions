"""``error_detail``: as much of a provider failure as a log can usefully carry.

``str()`` on an azure-core ``HttpResponseError`` is just
``Operation returned an invalid status 'Bad Request'`` — the *reason* is in the response
body, which that string drops.  A 400 that says only "Bad Request" cannot be acted on: it
took a hand-written probe against the live service to discover that one such failure meant
the app's identity had no role on the sandbox group.

This started life inside the bicep kind, the only caller that needed it.  The ACAS backend's
own warning logs have the identical gap — a bare ``%s`` of the exception, dropping the same
response body — so it moved here where every backend and every kind can reach it, rather than
being copied.

Duck-typed on purpose, and stdlib-only: it reads ``status_code`` and ``response.text()`` off
whatever it is given, which is how it works against azure-core's ``HttpResponseError`` — or
any other SDK's exception shaped the same way — without importing it.  This is a log-only
utility; the caller decides separately what a model or end user is told, and that message
must stay sanitized regardless of what this function returns.
"""

from __future__ import annotations

__all__ = ["error_detail"]


def error_detail(exc: BaseException) -> str:
    """As much of a failure as the log can usefully carry.

    ``str()`` on an azure-core ``HttpResponseError`` is just
    ``Operation returned an invalid status 'Bad Request'`` — the *reason* is in the response
    body, which that string drops.  A 400 that says only "Bad Request" cannot be acted on:
    it took a hand-written probe against the live service to discover that one such failure
    meant the app's identity had no role on the sandbox group.  This is log-only; the model
    still sees the sanitized message.

    **It raises only what a process may not swallow.**  Every caller is already handling a
    failure, and several are handling one they have promised to contain — an observer's, a
    disposal's — so a diagnostic that raised would replace the failure it was describing with
    itself, at the one moment nobody is in a position to absorb it.  Rendering an exception runs
    *its* code: ``__str__`` and a property like ``status_code`` are the exception author's, not
    this package's, so anything at all can come out of them.  ``SystemExit`` and
    ``KeyboardInterrupt`` still escape, matching what the containment sites themselves let
    through; everything else, ``CancelledError`` and ``GeneratorExit`` included, is contained.
    """
    try:
        return _rendered(exc)
    except (SystemExit, KeyboardInterrupt):
        # The host's own control flow, and the two things a diagnostic may never swallow. Every
        # other escape is contained below, including the ones that are not `Exception`: a
        # `__str__` that raises `CancelledError` would otherwise walk straight out of the
        # handler that called this to contain something.
        raise
    except BaseException:  # noqa: BLE001 - diagnostics must not raise; see the docstring
        # The class name is the one thing that cannot fail to render.
        return type(exc).__name__


def _rendered(exc: BaseException) -> str:
    """The detail itself — see :func:`error_detail`, which is where the never-raises duty is."""
    parts = [f"{type(exc).__name__}: {exc}"]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.text()
        except Exception:  # noqa: BLE001 - diagnostics must not raise
            body = None
        if body:
            parts.append(f"body={body[:600]}")
    return " | ".join(parts)
