"""Collect independent fixture cleanup failures without losing their ownership records."""

from __future__ import annotations


class Cleanup:
    """Attempt every independent cleanup and close; raise all failures on exit."""

    def __init__(self, *close):
        self.close = close
        self.errors = []

    def __enter__(self):
        return self

    def attempt(self, record, operation, *args):
        try:
            operation(*args)
        except Exception as exc:
            exc.add_note(f"Retained cleanup record: {record!r}")
            self.errors.append(exc)

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.errors.append(exc)
        for close in self.close:
            self.attempt("client connection", close)
        if self.errors:
            raise BaseExceptionGroup(
                "fixture cleanup incomplete; retain listed identities", self.errors
            )
