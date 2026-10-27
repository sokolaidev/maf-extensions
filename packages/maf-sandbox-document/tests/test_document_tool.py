"""Document tool admission, binary provenance, output completeness and cleanup."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    CallerContext,
    Capability,
    ExecResult,
    Isolation,
    IsolationScope,
    ListedFile,
    OsFamily,
    SandboxRouter,
    SourceIntegrity,
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_document import document_sandbox_spec, make_document_tools


class Store:
    def __init__(self, value=b"{\\rtf1 hello}"):
        self.value = value
        self.reads = []

    async def read(self, name):
        self.reads.append(name)
        return self.value

    async def list(self):
        return [ListedFile("input.rtf", SourceIntegrity.UNTRUSTED)]


class Converter(InProcessSandbox):
    def __init__(self, *, exit_code=0, missing=False):
        super().__init__()
        self.exit_code = exit_code
        self.missing = missing

    async def exec(self, command, *, working_directory, timeout):
        await super().exec(command, working_directory=working_directory, timeout=timeout)
        assert command == ["python3", "-I", "_guest.py"]
        root = self._working_directory(working_directory)
        assert self.contents[f"{root}/input.bin"] == b"{\\rtf1 hello}"
        metadata = dict(
            schema_version=1,
            format="text",
            verdict="extracted",
            truncated=False,
            pages=None,
            selected_pages=[],
            characters=5,
            files=["document.txt"],
            media_type="application/rtf",
            supported_formats=["text", "markdown"],
            range_clamped=False,
            reason=None,
        )
        if self.exit_code == 2:
            metadata.update(verdict="refused", files=[], characters=0, reason="test cap")
        self.contents[f"{root}/document.json"] = json.dumps(metadata).encode()
        if not self.missing:
            self.contents[f"{root}/document.txt"] = b"hello"
        return ExecResult(exit_code=self.exit_code, stderr="guest diagnostic")


def attach(tmp_path, sandbox=None, store=None, **kwargs):
    backend = InProcessSandboxBackend(
        sandbox or Converter(),
        isolation=Isolation.CONTAINER,
        sandbox_per_key=True,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    store = store or Store()
    context = CallerContext(
        current_scope=lambda: "document-tests",
        current_thread_id=lambda: "thread",
        list_files=lambda source: source.list(),
    )
    [tool] = make_document_tools(
        SandboxRouter([backend], min_isolation=Isolation.CONTAINER),
        store,
        "reader",
        context,
        make_file_system_sink(tmp_path),
        **kwargs,
    )
    return tool, backend, store


def invoke(tool, **kwargs):
    return asyncio.run(tool.func(file="input.rtf", **kwargs))


def verdict(answer):
    return next((item.text for item in answer if item.text.startswith("Result: ")), None)


def test_binary_store_staging_and_delivery_are_call_isolated(tmp_path):
    tool, backend, store = attach(tmp_path)
    answer = invoke(tool)
    assert answer[0].text == COMPLETED_TEXT
    assert verdict(answer) == "Result: extracted"
    assert (tmp_path / "document.txt").read_bytes() == b"hello"
    assert store.reads == ["input.rtf"]
    assert len(backend.disposed) == 1
    assert backend.specs[0].isolation_scope == IsolationScope.CALL
    assert answer[-1].additional_properties["security_label"]["integrity"] == "untrusted"


@pytest.mark.parametrize("exit_code,expected", [(2, "Result: refused"), (3, None), (137, None)])
def test_guest_status_and_diagnostics_are_separate(tmp_path, exit_code, expected):
    tool, backend, _ = attach(tmp_path, Converter(exit_code=exit_code))
    answer = invoke(tool)
    assert verdict(answer) == expected and backend.disposed
    assert answer[-1].additional_properties["security_label"]["integrity"] == "untrusted"


def test_missing_output_never_reports_extraction(tmp_path):
    tool, backend, _ = attach(tmp_path, Converter(missing=True))
    answer = invoke(tool)
    assert answer[0].text != COMPLETED_TEXT and verdict(answer) is None
    assert backend.disposed


@pytest.mark.parametrize("pages", ["0", "-1", "5-2", "1;whoami", "", True, 1])
def test_invalid_ranges_do_not_read_or_acquire(tmp_path, pages):
    tool, backend, store = attach(tmp_path)
    answer = invoke(tool, pages=pages)
    assert answer[0].text != COMPLETED_TEXT and not store.reads and not backend.keys


def test_disabled_images_do_not_acquire(tmp_path):
    tool, backend, store = attach(tmp_path, formats=("text",))
    assert verdict(invoke(tool, format="images")) is None
    assert not backend.keys and not store.reads


def test_unlisted_name_is_not_read(tmp_path):
    tool, backend, store = attach(tmp_path)
    answer = asyncio.run(tool.func(file="../input.rtf"))
    assert verdict(answer) is None and not backend.keys and not store.reads


@pytest.mark.parametrize("value", [None, "text is not binary"])
def test_absent_or_text_input_does_not_acquire(tmp_path, value):
    tool, backend, _ = attach(tmp_path, store=Store(value))
    assert verdict(invoke(tool)) is None and not backend.keys


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_pages", True),
        ("max_pages", 0),
        ("max_chars", -1),
        ("image_dpi", 601),
        ("max_image_pixels", 0),
        ("max_image_pages", 101),
        ("exec_timeout_seconds", float("nan")),
        ("exec_timeout_seconds", 301),
        ("formats", ()),
        ("formats", ("text", "text")),
    ],
)
def test_invalid_host_configuration(tmp_path, option, value):
    with pytest.raises(ValueError):
        attach(tmp_path, **{option: value})


def test_spec_requires_closed_egress_and_disposal():
    from maf_sandbox import Cleanup, Egress

    spec = document_sandbox_spec()
    assert spec.egress == Egress.CLOSED and spec.min_cleanup == Cleanup.DISPOSE
    assert spec.requires == {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
