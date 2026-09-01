"""The egress proxy is a copy, and it must stay byte-identical to maf-sandbox-wslc's.

The maintainer ruling was: each backend ships its own copy of the filtering proxy rather than a
hoisted shared one, so `maf-sandbox` core stays free of operational components. The cost of that
choice is a mirrored copy that could silently drift; this test is the price paid to make drift
loud. A fix to the private-address refusal made in one package without the other must fail here,
not in production.

**It belongs to neither package, which is why it sits here.** The claim spans two of them and
is true or false whatever a consumer installs, so it is a repository invariant like the ones
beside it, not part of `maf-sandbox-docker`'s shipped suite. Living under that package meant
`check_dependent_works_with_published_cores.py` dragged it into a throwaway wheel-only
environment on every published core it tests, where the sibling need not exist at all; that it
passed there for three weeks was luck, and it ran out the first time no published
`maf-sandbox-wslc` admitted the core under test. Here both packages are always installed, so
the guard below is unconditional rather than contingent on what the index happens to hold.
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
        """A guard so an absent sibling does not let the parity check pass vacuously."""
        assert _wslc_context() is not None, (
            "maf-sandbox-wslc is not importable, so proxy parity cannot be checked. Every test "
            "in this tree runs against the workspace, where it always is."
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
