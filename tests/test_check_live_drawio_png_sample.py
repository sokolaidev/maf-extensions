"""Verify that the draw.io PNG live check requires artifacts from a clean call."""

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _sample_agent import load_sample
from maf_sandbox import (
    DisposalFailure,
    Isolation,
    IsolationScope,
    SandboxAcquired,
    SandboxDisposed,
    SandboxKey,
    SandboxSpec,
    ToolCallEnded,
)
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_live_drawio_png_sample import verify  # noqa: E402


@pytest.fixture
def agent() -> ModuleType:
    return load_sample("25_docker_drawio_png")


@pytest.mark.parametrize("outcome", ["gone", "missing", "may_remain", "unknown", "wrong_call"])
def test_call_cleanup_requires_a_successful_disposal_after_acquiring_the_sandbox(
    agent: ModuleType, outcome: str
) -> None:
    observer = agent.ExportObserver()
    key = SandboxKey(scope="sample", thread_id="thread", agent_id="author", call_id="call-1")
    disposed = SandboxDisposed(
        key=key, backend="docker", outcome="gone", failure=None, seconds=0.1, call="call-1"
    )
    observer.sandbox_disposed(disposed)
    observer.sandbox_acquired(
        SandboxAcquired(
            key=key,
            spec=SandboxSpec(kind="drawio"),
            isolation_scope=IsolationScope.CALL,
            backend="docker",
            isolation=Isolation.CONTAINER,
            declarations=None,
            seconds=0.1,
            call="call-1",
        )
    )
    if outcome == "gone":
        observer.sandbox_disposed(disposed)
    elif outcome == "may_remain":
        observer.sandbox_disposed(
            replace(disposed, outcome="may_remain", failure=DisposalFailure("refused", "busy"))
        )
    elif outcome == "unknown":
        observer.sandbox_disposed(replace(disposed, outcome="unknown"))
    elif outcome == "wrong_call":
        observer.sandbox_disposed(replace(disposed, call="call-2"))
    ended = ToolCallEnded(
        tool="create_drawio",
        kind="drawio",
        keys=(key,),
        seconds=0.5,
        failure=None,
        unclean=0,
        call="call-1",
    )
    observer.tool_call_ended(ended)
    observer.sandbox_disposed(replace(disposed, call=None))
    if outcome == "gone":
        assert observer.clean_call() == (ended, disposed)
    else:
        with pytest.raises(RuntimeError, match="per-call sandbox disposal"):
            observer.clean_call()


@pytest.fixture
def export(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    drawio = tmp_path / "diagram.drawio"
    drawio.write_text("<mxfile/>", encoding="utf-8")
    png = tmp_path / "diagram-1.png"
    with Image.new("RGB", (40, 20)) as image:
        image.save(png)
    return tmp_path, {
        "tool": "create_drawio",
        "call": "call-1",
        "seconds": 0.5,
        "unclean": 0,
        "call_disposal": {"call": "call-1", "outcome": "gone", "failure": None},
        "undisposed": False,
        "width": 40,
        "height": 20,
        "drawio_sha256": hashlib.sha256(drawio.read_bytes()).hexdigest(),
        "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
    }


def test_accepts_a_clean_export(export: tuple[Path, dict[str, object]]) -> None:
    output, evidence = export
    verify(f"  [measured] {json.dumps(evidence)}", output)


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "export_drawio"},
        {"call": ""},
        {"unclean": 1},
        {"call_disposal": None},
        {"call_disposal": {"call": "call-2", "outcome": "gone", "failure": None}},
        {"call_disposal": {"call": "call-1", "outcome": "unknown", "failure": None}},
        {"call_disposal": {"call": "call-1", "outcome": "gone", "failure": "backend failure"}},
        {"undisposed": True},
        {"seconds": -1},
        {"width": 41},
        {"drawio_sha256": "wrong"},
        {"png_sha256": "wrong"},
    ],
)
def test_rejects_mismatched_evidence(
    export: tuple[Path, dict[str, object]], change: dict[str, object]
) -> None:
    output, evidence = export
    with pytest.raises(ValueError):
        verify(f"  [measured] {json.dumps(evidence | change)}", output)


def test_rejects_replaced_png(export: tuple[Path, dict[str, object]]) -> None:
    output, evidence = export
    with Image.new("RGB", (40, 20), color="blue") as image:
        image.save(output / "diagram-1.png")
    with pytest.raises(ValueError, match="differ from the measured export"):
        verify(f"  [measured] {json.dumps(evidence)}", output)
