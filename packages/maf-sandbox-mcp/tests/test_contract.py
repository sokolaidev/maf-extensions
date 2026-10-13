"""Installed worker boundaries and fixed operation contract."""

import asyncio
import importlib.metadata

import pytest

from maf_sandbox_mcp import server


def test_fixed_entry_point_has_no_plugin_selector():
    entry = next(
        e
        for e in importlib.metadata.distribution("maf-sandbox-mcp").entry_points
        if e.name == "maf-bicep-mcp"
    )
    assert entry.load() is server.main


def test_composition_is_inert_until_service_start():
    async def check():
        app = await server.http_application("sha256:" + "a" * 64, "{}", "test", "a" * 64, 8765)
        assert set(app.service.bindings) == {"bicep_validate"}
        assert app.service.active is None
        assert app.sessions == {}
        binding = app.service.bindings["bicep_validate"]
        assert binding.tool.outputSchema == server.OUTPUT_SCHEMA
        assert binding.tool.inputSchema["additionalProperties"] is False

    asyncio.run(check())


@pytest.mark.parametrize(
    "path", ["../main.bicep", "C:/main.bicep", "dir/../main.bicep", "main.json"]
)
def test_inline_input_refuses_host_paths_and_auxiliary_files(path):
    with pytest.raises(ValueError):
        server.snapshot({"files": [{"path": path, "content": ""}]})
