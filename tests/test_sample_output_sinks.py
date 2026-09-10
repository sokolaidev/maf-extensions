"""Shared-root sample sinks replace repeated names and record only actual deliveries."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from maf_sandbox import Artifact, OutputSink

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_RECORDING_SAMPLES = ("08_docker_codeact_files", "14_acas_codeact_files")


def _load_agent(sample: str, monkeypatch: pytest.MonkeyPatch):
    directory = _SAMPLES / sample
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(f"sample_{sample}", directory / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagram_agent_wires_a_sink_that_replaces_previous_output(tmp_path, monkeypatch):
    agent = _load_agent("07_docker_diagram", monkeypatch)
    monkeypatch.setattr(agent, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(agent, "require_env_vars", lambda names: dict.fromkeys(names, "unused"))
    sinks: list[OutputSink] = []

    def capture_sink(router, agent_dir, context, sink, **kwargs):
        sinks.append(sink)
        return []

    monkeypatch.setattr(agent, "make_diagram_tools", capture_sink)
    assert asyncio.run(agent.run()) == 2
    [sink] = sinks
    destination = tmp_path / "diagram.png"
    destination.write_bytes(b"previous image")

    async def deliver() -> None:
        landed = await sink.deliver(Artifact("diagram.png", b"new image", "diagram", "image/png"))
        assert landed.name == "diagram.png"

    asyncio.run(deliver())
    assert destination.read_bytes() == b"new image"


@pytest.mark.parametrize("sample", _RECORDING_SAMPLES)
def test_recording_sink_replaces_across_calls_and_turns(sample, tmp_path, monkeypatch):
    agent = _load_agent(sample, monkeypatch)
    untouched = tmp_path / "other.md"
    untouched.write_bytes(b"other output")
    destination = tmp_path / "summary.md"
    destination.write_bytes(b"earlier turn")
    delivered: list[str] = []
    sink = agent.make_recording_sink(tmp_path, delivered)

    async def deliver(content: bytes) -> None:
        landed = await sink.deliver(Artifact("summary.md", content, "codeact", "text/markdown"))
        assert landed.name == "summary.md"
        assert destination.read_bytes() == content

    asyncio.run(deliver(b"first attempt"))
    asyncio.run(deliver(b"corrected attempt"))
    assert delivered == ["summary.md", "summary.md"]

    delivered = []
    sink = agent.make_recording_sink(tmp_path, delivered)
    assert delivered == []
    asyncio.run(deliver(b"next turn"))
    assert delivered == ["summary.md"]
    assert untouched.read_bytes() == b"other output"
