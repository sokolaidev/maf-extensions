"""Fixed result metadata describes text without changing rendering or integrity."""

import asyncio

import pytest
from agent_framework import Content

from maf_sandbox import CallerContext, Isolation, SandboxRouter, SandboxSpec
from maf_sandbox.maf import (
    _SESSION_FIXED_SENTENCES,
    COMPLETED_TEXT,
    NOT_COMPLETED_TEXT,
    SandboxResult,
    sandboxed_tool,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend


def _attach(*, fixed=(), guidance=(), contract=True, verdicts=(), answer=None, declarations=None):
    def build(session):
        async def probe():
            """Return the prepared result."""
            return answer if answer is not None else SandboxResult(completed=False)

        return probe

    return sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", InMemoryStore.list),
        spec=SandboxSpec(kind="probe", work_dir="/work"),
        name="probe",
        agent_id="agent",
        source_integrity="untrusted" if declarations is None else None,
        declarations=declarations,
        result_contract=contract,
        standing_guidance=guidance,
        fixed_sentences=fixed,
        verdicts=verdicts,
    )[0]


def test_metadata_snapshots_generators_and_deduplicates_exact_text():
    fixed = ["Refused.", "Refused.", "Text with {literal braces}."]
    tool = _attach(
        fixed=iter(fixed),
        guidance=iter(("Guidance.", "Literal {{braces}}.", "Saved under {call_id}/.")),
        verdicts=iter(("valid", False, 2)),
    )
    fixed.append("Added later.")
    assert tool.additional_properties["fixed_sentences"] == (
        COMPLETED_TEXT,
        NOT_COMPLETED_TEXT,
        "Result: valid",
        "Result: False",
        "Result: 2",
        "Guidance.",
        "Literal {braces}.",
        "Refused.",
        "Text with {literal braces}.",
        *_SESSION_FIXED_SENTENCES,
        "Error: no active thread context — probe must be called from within a thread",
    )
    assert "standing_guidance" not in tool.additional_properties


@pytest.mark.parametrize("fixed", ["single string", (1,), (None,)])
def test_invalid_entry_types_are_refused_at_attach(fixed):
    with pytest.raises(TypeError, match="fixed_sentences"):
        _attach(fixed=fixed)


@pytest.mark.parametrize("fixed", [("",), (" \n",)])
def test_empty_entries_are_refused_at_attach(fixed):
    with pytest.raises(ValueError, match="fixed_sentences"):
        _attach(fixed=fixed)


def test_metadata_preserves_whitespace_and_is_independent_per_tool():
    first = _attach(fixed=(" exact text ",))
    second = _attach()
    assert " exact text " in first.additional_properties["fixed_sentences"]
    assert " exact text " not in second.additional_properties["fixed_sentences"]
    first.additional_properties["fixed_sentences"] += ("Later.",)
    assert "Later." not in second.additional_properties["fixed_sentences"]


def test_matching_guest_text_is_still_untrusted_and_metadata_does_not_render():
    answer = SandboxResult(completed=True, output=("Same sentence.", "Guest text."))
    tool = _attach(fixed=("Same sentence.", "Metadata only."), answer=answer)
    tool.additional_properties["fixed_sentences"] += ("Guest text.",)
    items = asyncio.run(tool.invoke(arguments={}))
    assert [item.text for item in items] == [COMPLETED_TEXT, "Same sentence.", "Guest text."]
    for item in items[1:]:
        assert item.additional_properties["security_label"]["integrity"] == "untrusted"


def test_noncontract_tool_gets_only_declared_text_and_no_label_promotion():
    tool = _attach(
        fixed=("Fixed.",),
        contract=False,
        answer=[Content.from_text("Fixed.")],
        declarations={"source_integrity": "untrusted", "confidentiality": "public"},
    )
    declared = tool.additional_properties["fixed_sentences"]
    assert "Fixed." in declared and COMPLETED_TEXT not in declared
    assert tool.additional_properties["source_integrity"] == "untrusted"
    [item] = asyncio.run(tool.invoke(arguments={}))
    assert item.additional_properties["security_label"]["integrity"] == "untrusted"


def test_fixed_metadata_does_not_require_an_integrity_declaration():
    tool = _attach(fixed=("Fixed.",), contract=False, declarations={})
    assert "Fixed." in tool.additional_properties["fixed_sentences"]
    assert "source_integrity" not in tool.additional_properties


def test_declarations_cannot_override_the_catalogue():
    declarations = {"source_integrity": "untrusted", "fixed_sentences": ["Stale."]}
    tool = _attach(fixed=("Current.",), declarations=declarations)
    assert "Stale." not in tool.additional_properties["fixed_sentences"]
    assert "Current." in tool.additional_properties["fixed_sentences"]
    assert declarations["fixed_sentences"] == ["Stale."]
