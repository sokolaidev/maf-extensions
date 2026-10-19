"""Render through sample 07 with a real Graphviz sbx template and no model."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from maf_sandbox import SandboxRouter, make_file_system_sink
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "samples/07_docker_diagram"))
from diagram_kind import make_diagram_tools  # noqa: E402

_IMAGE = os.environ.get("MAF_GRAPHVIZ_SBX_E2E_IMAGE", "")
pytestmark = pytest.mark.skipif(not _IMAGE, reason="requires MAF_GRAPHVIZ_SBX_E2E_IMAGE and sbx")


def test_template_delivers_a_png_through_the_sample(tmp_path):
    async def scenario():
        scope = "graphviz-sbx-" + uuid4().hex
        backend = SbxSandboxBackend(
            SbxSandboxConfig(
                sbx_path=os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx"),
                workspace_root=tmp_path / "workspaces",
            )
        )
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        [tool] = make_diagram_tools(
            router,
            "renderer",
            make_caller_context(list_no_files, lambda: scope, lambda: "thread"),
            make_file_system_sink(tmp_path / "out"),
            image=_IMAGE,
        )
        render = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        try:
            result = await render(dot="digraph { input -> renderer -> output }")
            output = tmp_path / "out/diagram.png"
            assert output.is_file(), result
            png = output.read_bytes()
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            assert png[12:16] == b"IHDR"
            width, height = struct.unpack(">II", png[16:24])
            assert width > 0 and height > 0
        finally:
            disposed = await backend.dispose_scope(scope, "thread")
            assert not disposed.undisposed, disposed

    asyncio.run(scenario())
