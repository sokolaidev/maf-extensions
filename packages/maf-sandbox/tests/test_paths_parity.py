"""`guest_directory_chain` is a copy, and it must stay in step with the backends' private ones.

`maf-sandbox-acas` and `maf-sandbox-docker` each carry a `_directory_chain` the promoted version
was lifted from. They cannot adopt it until a core release moves their pinned `maf-sandbox` floor
past the one that added it, so three copies stand until then; this test is the price paid to make
drift loud. A fix to the ancestor walk made in one package without the others must fail here, not
in production.

The backends are this package's *dependents*, so they are imported defensively — a wheel-only
install has neither, and skipping is the right answer there. In the workspace both are present
and every case below runs.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest

from maf_sandbox.paths import guest_directory_chain

_BACKENDS = ("maf_sandbox_acas._backend", "maf_sandbox_docker._backend")

# Non-normalised bases are half the point: `working_directory` is a plain string nothing
# normalises upstream, and it is where two copies of this walk would most easily disagree.
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


def _backend_chain(module_name: str) -> Callable[[str, str], tuple[str, ...]] | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return module._directory_chain


class TestGuestDirectoryChainMatchesTheBackendCopies:
    @pytest.mark.parametrize("module_name", _BACKENDS)
    @pytest.mark.parametrize(("guest_path", "working_directory"), _CASES)
    def test_the_promoted_walk_answers_what_the_backend_answers(
        self, module_name: str, guest_path: str, working_directory: str
    ):
        theirs = _backend_chain(module_name)
        if theirs is None:
            pytest.skip(f"{module_name} is not importable in this environment")
        assert guest_directory_chain(guest_path, working_directory) == theirs(
            guest_path, working_directory
        ), (
            f"{module_name}._directory_chain disagrees with maf_sandbox.paths."
            "guest_directory_chain. The copies are a deliberate mirror until the backends' "
            "dependency floor lets them import the shared one, so a change to either must be "
            "made to both in the same breath, or this fails."
        )
