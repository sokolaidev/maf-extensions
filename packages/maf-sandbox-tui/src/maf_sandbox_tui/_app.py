"""Textual operator console for cooperative maf-sandbox control endpoints."""

from __future__ import annotations

import time
from typing import cast

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from ._client import PartialInventoryError
from ._control import SandboxControl
from ._models import DisposalStatus, SandboxRecord, SandboxState

_STATE_STYLE = {
    SandboxState.STARTING: "#67d4ff",
    SandboxState.READY: "#7bd88f",
    SandboxState.RUNNING: "#f2c14e",
    SandboxState.RESETTING: "#67d4ff",
    SandboxState.DISPOSING: "#aab8c2",
    SandboxState.FAILED: "#ff6b6b",
}


def _age(stamp: float, *, now: float | None = None) -> str:
    seconds = max(0, int((time.time() if now is None else now) - stamp))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _state_cell(state: SandboxState) -> Content:
    return Content.assemble(("● ", _STATE_STYLE[state]), (state.value.upper(), "bold"))


def _detail(record: SandboxRecord) -> Content:
    state_style = _STATE_STYLE[record.state]
    rail = Content.assemble(
        ("○", "#586773"),
        ("━━", "#586773"),
        ("●", state_style),
        (f"  {record.state.value.upper()}\n", f"bold {state_style}"),
    )
    detail = Content().append(rail)
    rows = (
        ("SOURCE", record.source_id),
        ("BACKEND", record.backend),
        ("KEY", record.logical_name),
        ("KIND", record.kind),
        ("INSTANCE", record.instance_id),
        ("PROCESS", "—" if record.process_id is None else str(record.process_id)),
        ("AGE", _age(record.created_at)),
        ("LAST SIGNAL", f"{_age(record.last_activity_at)} ago"),
        ("CONTRACT", record.execution_contract or "—"),
        ("EGRESS", "closed" if not record.egress_targets else "\n".join(record.egress_targets)),
    )
    for label, value in rows:
        detail = detail.append_text(f"\n{label:<12}", style="bold #7990a0")
        detail = detail.append_text(value, style="#d8e1e8")
    return detail


class ConfirmDelete(ModalScreen[str | None]):
    """Require an explicit operator choice before disposal."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, record: SandboxRecord) -> None:
        super().__init__()
        self.record = record

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-card"):
            yield Label("DISPOSE PHYSICAL INSTANCE", id="confirm-title")
            yield Static(
                Content.assemble(
                    ("Dispose ", "#aab8c2"),
                    (self.record.logical_name, "bold #f6f8fa"),
                    ("?\n\n", "#aab8c2"),
                    (self.record.instance_id, "#67d4ff"),
                    ("\n\nA newer generation with the same key is protected.", "#aab8c2"),
                )
            )
            with Horizontal(id="confirm-actions"):
                yield Button("Keep sandbox", id="cancel")
                yield Button("Dispose", id="dispose", variant="error")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.record.instance_id if event.button.id == "dispose" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SandboxConsole(App[None]):
    """List, inspect and dispose sandboxes reported by cooperative MAF hosts."""

    TITLE = "MST"
    SUB_TITLE = "MAF sandbox signals"
    CSS = """
    $panel: #18212a;
    $ink: #d8e1e8;
    $muted: #7990a0;
    $signal: #67d4ff;

    Screen {
        background: #10161c;
        color: $ink;
    }

    Header {
        background: #10161c;
        color: $ink;
        border-bottom: solid #2b3a45;
    }

    #body {
        height: 1fr;
        padding: 1 2;
    }

    #inventory-panel {
        width: 2fr;
        min-width: 64;
        background: $panel;
        border: solid #2b3a45;
    }

    #detail-panel {
        width: 1fr;
        min-width: 34;
        margin-left: 1;
        padding: 1 2;
        background: #131b22;
        border-left: thick $signal;
        overflow-y: auto;
    }

    #body.compact {
        layout: vertical;
    }

    #body.compact #inventory-panel {
        width: 1fr;
        min-width: 0;
        height: 1fr;
    }

    #body.compact #detail-panel {
        width: 1fr;
        min-width: 0;
        height: 1fr;
        margin-left: 0;
        margin-top: 1;
    }

    #detail-title, #inventory-title {
        height: 2;
        padding: 0 1;
        color: $muted;
        text-style: bold;
    }

    #sandboxes {
        height: 1fr;
        background: $panel;
    }

    DataTable > .datatable--cursor {
        background: #263744;
        color: #f6f8fa;
    }

    #status {
        height: 1;
        padding: 0 2;
        background: #0b1015;
        color: $muted;
    }

    Footer {
        background: #10161c;
    }

    ConfirmDelete {
        align: center middle;
        background: #000000 55%;
    }

    #confirm-card {
        width: 66;
        height: auto;
        padding: 1 2;
        background: #18212a;
        border: thick #ff6b6b;
    }

    #confirm-title {
        color: #ff8a8a;
        text-style: bold;
        margin-bottom: 1;
    }

    #confirm-actions {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #confirm-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "delete", "Dispose"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, control: SandboxControl, *, refresh_interval: float = 2.0) -> None:
        super().__init__()
        self.control = control
        self.refresh_interval = refresh_interval
        self.records: dict[str, SandboxRecord] = {}
        self.selected_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="inventory-panel"):
                yield Label("LIVE INSTANCES", id="inventory-title")
                yield DataTable[object](id="sandboxes", zebra_stripes=True)
            with Vertical(id="detail-panel"):
                yield Label("PHYSICAL SANDBOX", id="detail-title")
                yield Static("Select a sandbox to inspect its current generation.", id="detail")
        yield Static("Waiting for a sandbox signal…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = cast("DataTable[object]", self.query_one("#sandboxes", DataTable))
        table.cursor_type = "row"
        table.add_columns("STATE", "SOURCE", "MAF KEY", "KIND", "AGE", "INSTANCE")
        self.action_refresh()
        self.set_interval(self.refresh_interval, self.action_refresh)

    def on_resize(self, event: Resize) -> None:
        """Stack details below the inventory when a side-by-side console would clip them."""
        self.query_one("#body").set_class(event.size.width < 103, "compact")

    def action_refresh(self) -> None:
        self.run_worker(self._refresh(), group="inventory", exclusive=True)

    async def _refresh(self) -> None:
        status = self.query_one("#status", Static)
        partial: PartialInventoryError | None = None
        try:
            snapshot = await self.control.list_sandboxes()
        except PartialInventoryError as error:
            snapshot = error.records
            partial = error
        except Exception as error:
            status.update(Content.assemble((f"Control endpoint unavailable · {error}", "#ff6b6b")))
            return
        self.records = {record.instance_id: record for record in snapshot}
        table = cast("DataTable[object]", self.query_one("#sandboxes", DataTable))
        table.clear(columns=False)
        for record in snapshot:
            table.add_row(
                _state_cell(record.state),
                record.source_id,
                record.logical_name,
                record.kind,
                _age(record.created_at),
                record.instance_id[:8],
                key=record.instance_id,
            )
        if self.selected_id not in self.records:
            self.selected_id = snapshot[0].instance_id if snapshot else None
        self._show_selected()
        noun = "instance" if len(snapshot) == 1 else "instances"
        message = f"{len(snapshot)} live {noun} · refreshed just now"
        if partial is None:
            status.update(message)
        else:
            status.update(
                Content.assemble(
                    (f"{message} · {len(partial.errors)} host(s) unavailable", "#f2c14e")
                )
            )

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self.selected_id = key if isinstance(key, str) else None
        self._show_selected()

    def _show_selected(self) -> None:
        panel = self.query_one("#detail", Static)
        record = self.records.get(self.selected_id or "")
        if record is None:
            panel.update(
                Content.assemble(
                    (
                        "No live sandboxes.\n\nStart an opted-in MAF host or run mst --demo.",
                        "#7990a0",
                    )
                )
            )
        else:
            panel.update(_detail(record))

    def action_delete(self) -> None:
        record = self.records.get(self.selected_id or "")
        if record is None:
            self.query_one("#status", Static).update("Select a live sandbox first.")
            return
        self.push_screen(ConfirmDelete(record), self._delete_answered)

    def _delete_answered(self, instance_id: str | None) -> None:
        if instance_id is not None:
            self.run_worker(self._dispose(instance_id), group="disposal", exclusive=True)

    async def _dispose(self, instance_id: str) -> None:
        status = self.query_one("#status", Static)
        status.update(Content.assemble((f"Disposing {instance_id[:8]}…", "#f2c14e")))
        try:
            result = await self.control.dispose_sandbox(instance_id)
        except Exception as error:
            status.update(Content.assemble((f"Disposal failed · {error}", "#ff6b6b")))
            return
        style = "#7bd88f" if result.status is DisposalStatus.DISPOSED else "#ff6b6b"
        status.update(Content.assemble((result.message, style)))
        await self._refresh()
