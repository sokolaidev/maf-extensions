"""The egress proxy is a copy, and it must stay byte-identical to maf-sandbox-wslc's.

The maintainer ruling was: each backend ships its own copy of the filtering proxy rather than a
hoisted shared one, so `maf-sandbox` core stays free of operational components. The cost of that
choice is a mirrored copy that could silently drift; this test is the price paid to make drift
loud. A fix to the private-address refusal made in one package without the other must fail here,
not in production.

This runs in the workspace, where both packages are importable side by side; it is not shipped
in a way that requires wslc at a consumer's install time — a `pytest.skip` covers the wheel-only
case where the sibling is absent.
"""

from __future__ import annotations

import pathlib

import pytest

from maf_sandbox_docker import proxy_build_context

_FILES = ("Dockerfile", "proxy.py")


def _wslc_context() -> pathlib.Path | None:
    try:
        from maf_sandbox_wslc import proxy_build_context as wslc_context
    except ImportError:
        return None
    return wslc_context()


class TestProxyIsByteIdenticalToWslc:
    def test_the_sibling_is_available_in_this_workspace(self):
        """A guard so a skipped sibling does not let the parity check pass vacuously in CI."""
        # In the workspace both packages are installed; only a wheel-only install lacks the
        # sibling, and that is not where this test is meant to run.
        assert _wslc_context() is not None, (
            "maf-sandbox-wslc is not importable, so proxy parity cannot be checked. This test "
            "is meant to run in the workspace where both packages are present."
        )

    @pytest.mark.parametrize("filename", _FILES)
    def test_each_proxy_file_matches_wslcs_byte_for_byte(self, filename: str):
        wslc = _wslc_context()
        if wslc is None:
            pytest.skip("maf-sandbox-wslc is not importable in this environment")
        ours = (proxy_build_context() / filename).read_bytes()
        theirs = (wslc / filename).read_bytes()
        assert ours == theirs, (
            f"{filename} differs from maf-sandbox-wslc's copy. The two are a deliberate "
            "mirror (maintainer ruling: no hoist into core), so a change to one must be made "
            "to the other in the same breath, or this fails."
        )

    def test_the_build_context_holds_exactly_the_expected_files(self):
        present = {p.name for p in proxy_build_context().iterdir() if p.is_file()}
        # __init__.py makes the directory a subpackage; the two proxy files are the payload.
        assert {"Dockerfile", "proxy.py"} <= present
