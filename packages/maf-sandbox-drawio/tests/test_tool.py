"""The real converter behind MAF attachment and protocol artifact delivery."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    ExecResult,
    Isolation,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)

from maf_sandbox_drawio import drawio_sandbox_spec, make_drawio_tools

_XML = (
    '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="a" parent="1" vertex="1" value="Résumé &amp; 中文">'
    '<mxGeometry as="geometry" width="160" height="80"/></mxCell></root></mxGraphModel>'
)


class ConverterSandbox(InProcessSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[list[str], str, float]] = []

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        await super().exec(command, working_directory=working_directory, timeout=timeout)
        assert not isinstance(command, str)
        argv = list(command)
        assert argv[:3] == ["python3", "-I", "renderer.py"]
        self.calls.append((argv, working_directory, timeout))
        guest_directory = self._working_directory(working_directory)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("input.xml", "renderer.py"):
                Path(directory, name).write_bytes(self.contents[f"{guest_directory}/{name}"])
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, *argv[1:]],
                cwd=directory,
                timeout=timeout,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            output = Path(directory, "diagram.drawio")
            if output.exists():
                self.contents[f"{guest_directory}/diagram.drawio"] = output.read_bytes()
        return ExecResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)


def attach(sandbox: InProcessSandbox, output: Path, **kwargs):
    backend = InProcessSandboxBackend(
        sandbox,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS, capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    context = make_caller_context(list_no_files, lambda: "tests", lambda: "drawio")
    tools = make_drawio_tools(
        router,
        "diagram-designer",
        context,
        make_file_system_sink(output, existing="replace"),
        **kwargs,
    )
    assert len(tools) == 1
    return tools[0], backend


def invoke(tool, source: str = _XML) -> str:
    return asyncio.run(tool.func(xml=source))


def test_complete_tool_call_lands_native_xml_and_disposes(tmp_path: Path):
    sandbox = ConverterSandbox()
    tool, backend = attach(sandbox, tmp_path / "out")
    assert tool.name == "create_drawio"
    assert tool.additional_properties == {"source_integrity": "untrusted"}
    result = invoke(tool)
    assert result.startswith("diagram.drawio (") and result.endswith(" bytes)")
    document = ET.fromstring((tmp_path / "out/diagram.drawio").read_bytes())
    assert document.find(".//mxCell[@id='a']").get("value") == "Résumé & 中文"
    assert backend.disposed
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0][0][3:7] == ["--preserve-layout", "true", "--direction", "TB"]


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is not installed")
@pytest.mark.parametrize("preserve", [True, False])
def test_layout_configuration_and_missing_geometry_through_tool(tmp_path: Path, preserve: bool):
    sandbox = ConverterSandbox()
    tool, _ = attach(
        sandbox, tmp_path, preserve_layout=preserve, direction="LR", exec_timeout_seconds=20
    )
    source = _XML.replace('<mxGeometry as="geometry" width="160" height="80"/>', "")
    assert not invoke(tool, source).startswith("Error:")
    document = ET.fromstring((tmp_path / "diagram.drawio").read_bytes())
    assert document.find(".//mxGeometry").get("x") is not None
    assert sandbox.calls[0][0][3:7] == [
        "--preserve-layout",
        str(preserve).lower(),
        "--direction",
        "LR",
    ]
    assert sandbox.calls[0][2] == 20


def test_calls_use_distinct_directories_and_can_replace_output(tmp_path: Path):
    sandbox = ConverterSandbox()
    tool, _ = attach(sandbox, tmp_path)
    invoke(tool)
    invoke(tool, _XML.replace("Résumé", "Updated"))
    assert len({directory for _, directory, _ in sandbox.calls}) == 2
    assert "Updated" in (tmp_path / "diagram.drawio").read_text("utf-8")


def test_malformed_xml_delivers_a_diagnostic_and_no_file(tmp_path: Path):
    tool, backend = attach(ConverterSandbox(), tmp_path)
    assert "Invalid XML" in invoke(tool, "<mxfile>")
    assert not (tmp_path / "diagram.drawio").exists()
    assert backend.disposed


def test_oversized_input_does_not_acquire(tmp_path: Path):
    sandbox = ConverterSandbox()
    tool, backend = attach(sandbox, tmp_path)
    assert "1 MiB" in invoke(tool, "é" * 600_000)
    assert not sandbox.calls and not backend.keys


@pytest.mark.parametrize(
    "failure,expected",
    [(TimeoutError(), "timed out"), (RuntimeError("private-transport-account"), "could not run")],
)
def test_execution_failures_are_sanitized(tmp_path: Path, failure: Exception, expected: str):
    tool, _ = attach(InProcessSandbox(raises=failure), tmp_path)
    result = invoke(tool)
    assert expected in result
    assert "private-transport-account" not in result
    assert not (tmp_path / "diagram.drawio").exists()


def test_success_without_an_output_is_not_reported_as_saved(tmp_path: Path):
    tool, _ = attach(InProcessSandbox(), tmp_path)
    assert invoke(tool).startswith("Error:")


def test_converter_diagnostic_is_bounded(tmp_path: Path):
    class FailedConverter(InProcessSandbox):
        async def exec(
            self, command: str | Sequence[str], *, working_directory: str, timeout: float
        ) -> ExecResult:
            await super().exec(command, working_directory=working_directory, timeout=timeout)
            return ExecResult(stdout="", stderr="x" * 10000, exit_code=2)

    tool, _ = attach(FailedConverter(), tmp_path)
    result = invoke(tool)
    assert len(result) < 2150 and result.startswith("Error:")
    assert result.endswith("x" * 2048) and "x" * 2049 not in result


@pytest.mark.parametrize(
    "kwargs,exception",
    [
        ({"preserve_layout": "true"}, TypeError),
        ({"preserve_layout": 1}, TypeError),
        ({"direction": "RL"}, ValueError),
        ({"exec_timeout_seconds": True}, ValueError),
        ({"exec_timeout_seconds": 0}, ValueError),
        ({"exec_timeout_seconds": float("nan")}, ValueError),
        ({"exec_timeout_seconds": 301}, ValueError),
    ],
)
def test_invalid_host_configuration_fails_at_attachment(tmp_path: Path, kwargs, exception):
    with pytest.raises(exception):
        attach(InProcessSandbox(), tmp_path, **kwargs)


def test_unconfigured_router_has_no_tools(tmp_path: Path):
    assert (
        make_drawio_tools(
            None,
            "agent",
            make_caller_context(list_no_files, lambda: "s", lambda: "t"),
            make_file_system_sink(tmp_path),
        )
        == []
    )


def test_spec_requires_file_delivery_and_closed_egress():
    spec = drawio_sandbox_spec("drawio-sandbox:test")
    assert spec.requires == {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
    assert spec.egress_allow == () and spec.outputs_named_at_call_time
    assert spec.files_out.max_files == 1 and spec.files_out.max_total_bytes == 2 * 1024 * 1024


def test_description_and_schema_expose_only_model_xml(tmp_path: Path):
    for preserve, sentence in (
        (True, "Preserve supplied page geometry"),
        (False, "Replace every page's layout"),
    ):
        tool, _ = attach(InProcessSandbox(), tmp_path, preserve_layout=preserve)
        assert sentence in tool.description
        assert list(tool.parameters()["properties"]) == ["xml"]
