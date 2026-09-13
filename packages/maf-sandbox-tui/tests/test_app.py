"""The operator flow exercised through Textual's headless terminal."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from maf_sandbox_tui import MemoryControl, SandboxConsole


def test_console_lists_details_and_disposes_after_confirmation():
    async def check() -> None:
        control = MemoryControl.demo(now=1_000)
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await pilot.pause()
            table = app.query_one("#sandboxes", DataTable)
            assert table.row_count == 3
            assert app.selected_id is not None
            selected = app.selected_id

            await pilot.press("d")
            await pilot.pause()
            await pilot.click("#dispose")
            await pilot.pause()

            assert table.row_count == 2
            assert all(record.instance_id != selected for record in await control.list_sandboxes())

    asyncio.run(check())


def test_console_stacks_details_in_a_standard_width_terminal():
    async def check() -> None:
        app = SandboxConsole(MemoryControl.demo(now=1_000), refresh_interval=3_600)

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            body = app.query_one("#body")
            inventory = app.query_one("#inventory-panel")
            detail = app.query_one("#detail-panel")

            assert body.has_class("compact")
            assert detail.region.x == inventory.region.x
            assert detail.region.y > inventory.region.y
            assert detail.region.width > 0
            assert detail.region.height > 0

    asyncio.run(check())
