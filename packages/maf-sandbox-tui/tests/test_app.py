"""The operator flow exercised through Textual's headless terminal."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from textual.content import Content
from textual.widgets import DataTable, Static

from maf_sandbox_tui import DisposalResult, MemoryControl, SandboxConsole, SandboxRecord
from maf_sandbox_tui._app import _detail, _state_cell
from maf_sandbox_tui._client import PartialInventoryError


def test_console_renderables_use_textual_content():
    record = asyncio.run(MemoryControl.demo(now=1_000).list_sandboxes())[0]

    assert isinstance(_state_cell(record.state), Content)
    detail = _detail(record)
    assert isinstance(detail, Content)
    assert record.instance_id in str(detail)


def test_console_lists_details_and_disposes_after_confirmation():
    async def check() -> None:
        control = MemoryControl.demo(now=1_000)
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await pilot.pause()
            table = app.query_one("#sandboxes", DataTable)
            assert not app.query_one("#body").has_class("compact")
            assert table.row_count == 3
            assert app.selected_id is not None
            selected = app.records[app.selected_id].instance_id

            await pilot.press("d")
            await pilot.pause()
            await pilot.click("#dispose")
            await pilot.pause()

            assert table.row_count == 2
            assert all(record.instance_id != selected for record in await control.list_sandboxes())

    asyncio.run(check())


def test_console_retains_duplicate_ids_and_disables_ambiguous_disposal():
    class RecordingControl(MemoryControl):
        def __init__(self, records: tuple[SandboxRecord, ...]) -> None:
            super().__init__()
            self.records = records
            self.disposals: list[str] = []

        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            return self.records

        async def dispose_sandbox(
            self, instance_id: str, *, timeout: float = 10.0
        ) -> DisposalResult:
            self.disposals.append(instance_id)
            return await super().dispose_sandbox(instance_id, timeout=timeout)

    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        control = RecordingControl((record, replace(record, source_id="second-host")))
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await pilot.pause()
            assert app.query_one("#sandboxes", DataTable).row_count == 2
            assert len(app.records) == 2
            assert app.ambiguous_ids == frozenset({record.instance_id})
            assert "disabled — duplicate physical ID" in str(
                app.query_one("#detail", Static).render()
            )

            await pilot.press("d")
            await pilot.pause()

            assert control.disposals == []
            assert "duplicate physical instance ID" in str(
                app.query_one("#status", Static).render()
            )

    asyncio.run(check())


def test_console_stacks_details_in_a_standard_width_terminal():
    async def check() -> None:
        app = SandboxConsole(MemoryControl.demo(now=1_000), refresh_interval=3_600)

        async with app.run_test(size=(102, 30)) as pilot:
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


def test_console_surfaces_partial_inventory():
    class PartialControl(MemoryControl):
        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            records = await super().list_sandboxes()
            raise PartialInventoryError(records, ("host stopped",))

    async def check() -> None:
        control = PartialControl(await MemoryControl.demo(now=1_000).list_sandboxes())
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await pilot.pause()
            assert app.query_one("#sandboxes", DataTable).row_count == 3
            assert "1 host(s) unavailable" in str(app.query_one("#status", Static).render())

    asyncio.run(check())


def test_console_coalesces_refreshes_while_one_is_in_flight():
    class BlockingControl(MemoryControl):
        def __init__(self, records: tuple[SandboxRecord, ...]) -> None:
            super().__init__()
            self.records = records
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return self.records

    async def check() -> None:
        control = BlockingControl(await MemoryControl.demo(now=1_000).list_sandboxes())
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await asyncio.wait_for(control.entered.wait(), timeout=1)
            app.action_refresh()
            app.action_refresh()
            await pilot.pause()

            assert control.calls == 1
            control.release.set()
            await pilot.pause()
            assert control.calls == 2
            assert not app._refresh_running
            assert app.query_one("#sandboxes", DataTable).row_count == 3

    asyncio.run(check())


def test_console_does_not_cancel_an_in_flight_disposal():
    class BlockingControl(MemoryControl):
        def __init__(self, records: tuple[SandboxRecord, ...]) -> None:
            super().__init__()
            self.records = records
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def dispose_sandbox(
            self, instance_id: str, *, timeout: float = 10.0
        ) -> DisposalResult:
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return await super().dispose_sandbox(instance_id, timeout=timeout)

    async def check() -> None:
        control = BlockingControl(await MemoryControl.demo(now=1_000).list_sandboxes())
        app = SandboxConsole(control, refresh_interval=3_600)

        async with app.run_test(size=(128, 38)) as pilot:
            await pilot.pause()
            instance_id = control.records[0].instance_id
            app._delete_answered(instance_id)
            await asyncio.wait_for(control.entered.wait(), timeout=1)
            app._delete_answered(instance_id)
            await pilot.pause()

            assert control.calls == 1
            assert "already in progress" in str(app.query_one("#status", Static).render())
            control.release.set()
            await pilot.pause()
            assert not app._disposal_running

    asyncio.run(check())
