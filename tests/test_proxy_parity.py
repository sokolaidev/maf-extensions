"""Keep the mirrored Docker and WSLC proxy build contexts byte-identical."""

from __future__ import annotations

import pathlib

import pytest
from maf_sandbox_docker import proxy_build_context

_FILES = ("Dockerfile", "entrypoint.sh", "iron.patch", "policy.py")


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
            f"{filename} differs from maf-sandbox-wslc's copy; update both proxy contexts."
        )

    def test_the_build_context_holds_exactly_the_expected_files(self):
        present = {p.name for p in proxy_build_context().iterdir() if p.is_file()}
        assert set(_FILES) <= present
