"""The promoted confinement helper is a copy, and it must stay in step with the backends' own.

`maf-sandbox-acas` and `maf-sandbox-docker` each carry their own guest-path confinement, which the
promoted version was lifted from. They cannot adopt the shared one until a core release moves their
pinned `maf-sandbox` floor past the one that added it, so the copies stand until then; this is the
price paid to make drift loud. A fix made in one package without the others must fail here, not in
production.

The ancestor walk was the other half and is gone from both backends, which is what this suite was
holding the line for: they call `maf_sandbox.paths.refuse_symlinked_parents` now, so there is no
second copy left to compare against and a mirror asserting nothing is worse than no mirror.

The backends are this package's *dependents*, so they are imported defensively: a wheel-only
install has neither, and skipping is right there. Only an absent backend skips. An import that
fails for any other reason — a backend present but broken — is re-raised, because reporting that
as a skipped parity case would hide exactly the breakage this suite exists to surface.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import ModuleType

import pytest

from maf_sandbox.paths import confine_guest_path

_BACKENDS = ("maf_sandbox_acas._backend", "maf_sandbox_docker._backend")

#: Each backend's confinement entry point, adapted to `(path, working_directory) -> str`. The
#: signatures genuinely differ — acas answers `(guest, relative)` and docker takes its arguments
#: the other way round — and pinning that here is half the value of the comparison.
_CONFINERS: dict[str, tuple[str, Callable[[ModuleType, str, str], str]]] = {
    "maf_sandbox_acas._backend": ("_confined", lambda m, path, wd: m._confined(path, wd)[0]),
    "maf_sandbox_docker._backend": ("_guest_path", lambda m, path, wd: m._guest_path(wd, path)),
}

# Non-normalised bases are half the point: `working_directory` is a plain string nothing
# normalises upstream, and it is where two copies of this logic would most easily disagree.
_CASES = (
    ("/work", "/work"),
    ("/work/out", "/work"),
    ("/a/b/work", "/a/b/work"),
    ("/a/b/work/out/deeper", "/a/b/work"),
    ("/out", "/"),
    ("/work/out", "/work/"),
    ("/a/b/work/out", "/a/b/work/"),
    ("/work/out", "/work/."),
    ("/a/b/work/out", "/a/b/work/."),
    ("/a/b/work/out", "/a/b/./work"),
    ("/a/b/work/out", "/a/b/sub/../work"),
    ("/etc/passwd", "/work"),
    ("/work2/out", "/work"),
    ("/work", "/work/sub"),
)

#: Relative and escaping spellings, for the confinement pair only — the walk documents that its
#: `guest_path` is already confined, so feeding it these would compare undefined behaviour.
_CONFINE_CASES = _CASES + (
    ("a.txt", "/work"),
    ("sub/../a.txt", "/work"),
    ("..", "/work"),
    ("../../etc/passwd", "/work/sub"),
    ("", "/work"),
    (".", "/work"),
    ("a\\b.txt", "/work"),
    ("/work/../etc/passwd", "/work"),
)


def _backend(module_name: str) -> ModuleType | None:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as missing:
        top = module_name.split(".", 1)[0]
        if missing.name in (module_name, top):
            return None
        raise


def _outcome(call: Callable[[], object]) -> object:
    try:
        return ("ok", call())
    except Exception as exc:
        return (type(exc).__name__, str(exc))


_DRIFTED = (
    "This is a deliberate mirror until the backends' dependency floor lets them import the "
    "shared one, so a change to either must be made to both in the same breath, or this fails."
)


class TestConfinementMatchesTheBackendCopies:
    @pytest.mark.parametrize("module_name", _BACKENDS)
    @pytest.mark.parametrize(("path", "working_directory"), _CONFINE_CASES)
    def test_the_promoted_confinement_answers_what_the_backend_answers(
        self, module_name: str, path: str, working_directory: str
    ):
        module = _backend(module_name)
        if module is None:
            pytest.skip(f"{module_name} is not installed in this environment")
        attribute, theirs = _CONFINERS[module_name]
        if not hasattr(module, attribute):
            pytest.skip(f"{module_name} no longer carries {attribute}")
        assert _outcome(lambda: confine_guest_path(path, working_directory)) == _outcome(
            lambda: theirs(module, path, working_directory)
        ), f"{module_name}'s confinement has drifted. {_DRIFTED}"
