"""The four-slot result: what the wrapper renders, what it labels, and what it refuses."""

import asyncio
from typing import Any

import pytest
from agent_framework import Content, FunctionInvocationContext
from agent_framework.security import LabelTrackingFunctionMiddleware

from maf_sandbox import (
    CallerContext,
    Isolation,
    ListedFile,
    SandboxRouter,
    SandboxSpec,
    SourceChannel,
    SourceIntegrity,
)
from maf_sandbox.maf import (
    COMPLETED_TEXT,
    DERIVED_INTEGRITY_PROPERTY,
    NOT_COMPLETED_TEXT,
    SandboxResult,
    SandboxToolSession,
    sandboxed_tool,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend

_GUIDANCE = "A result you cannot read is not a clean check."
_SPEC = SandboxSpec(kind="probe", work_dir="/work")
_VERDICTS = ("valid", "invalid")


def _attach(
    answer, *, verdicts=_VERDICTS, contract=True, source="untrusted", guidance=(), reads=()
):
    """A probe tool whose body answers with ``answer``, having read ``reads`` from the store."""
    store = InMemoryStore({"a": "content"})

    def build(session: SandboxToolSession):
        async def probe() -> Any:
            """Answer with the prepared result."""
            for level in reads:
                await session.read_file(store, ListedFile("a", level))
            return answer() if callable(answer) else answer

        return probe

    return sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        agent_id="agent",
        spec=_SPEC,
        name="probe",
        source_integrity=source,
        nothing_survives_from=(SourceChannel.FILE_STORE,) if source == "trusted" else (),
        standing_guidance=guidance,
        result_contract=contract,
        verdicts=verdicts,
    )[0]


def _call(tool):
    return asyncio.run(tool.invoke(arguments={}))


def _texts(result):
    return [item.text for item in result]


def _label(item):
    return (item.additional_properties or {}).get("security_label")


class TestWhatTheWrapperRenders:
    """One item per part, in order, and a label on the derived part alone."""

    def test_every_part_becomes_its_own_item_in_order(self):
        answer = SandboxResult(
            completed=True,
            verdict="invalid",
            established=("ran against the pinned image",),
            derived=("line one", "line two"),
        )
        items = _call(_attach(answer))
        assert _texts(items) == [
            COMPLETED_TEXT,
            "Result: invalid",
            "ran against the pinned image",
            "line one",
            "line two",
        ]

    def test_only_the_derived_items_carry_a_label(self):
        answer = SandboxResult(completed=True, verdict="valid", established=("e",), derived=("d",))
        completed, verdict, established, derived = _call(_attach(answer))
        assert _label(completed) is None
        assert _label(verdict) is None
        assert _label(established) is None
        assert _label(derived) == {"integrity": "untrusted", "confidentiality": "public"}

    def test_an_incomplete_run_says_so_and_needs_no_verdict(self):
        items = _call(_attach(SandboxResult(completed=False, derived=("timed out",))))
        assert _texts(items) == [NOT_COMPLETED_TEXT, "timed out"]

    def test_the_kind_s_own_claim_stays_readable_on_the_attached_tool(self):
        tool = _attach(SandboxResult(completed=True))
        assert tool.additional_properties["source_integrity"] == "trusted"
        assert tool.additional_properties[DERIVED_INTEGRITY_PROPERTY] == "untrusted"

    def test_committed_guidance_still_comes_last(self):
        answer = SandboxResult(completed=True, verdict="valid", derived=("d",))
        items = _call(_attach(answer, guidance=(_GUIDANCE,)))
        assert _texts(items) == [COMPLETED_TEXT, "Result: valid", "d", _GUIDANCE]
        assert _label(items[-1]) == {"integrity": "trusted", "confidentiality": "public"}

    @pytest.mark.parametrize(
        ("levels", "weak"),
        [
            ((), False),
            ((SourceIntegrity.TRUSTED,), False),
            ((SourceIntegrity.UNTRUSTED,), True),
            ((None,), True),
        ],
    )
    def test_a_weak_read_still_weakens_the_derived_part(self, levels, weak):
        """The per-call fold reaches the contract exactly as it reaches a text result."""
        answer = SandboxResult(completed=True, derived=("d",))
        items = _call(_attach(answer, source="trusted", reads=levels))
        label = _label(items[-1])
        assert label is not None
        assert label["integrity"] == ("untrusted" if weak else "trusted")


class TestAnUnlabelledItemTakesTheToolDeclaration:
    """The contract the trusted parts rest on, against the real middleware.

    Slots 1 to 3 carry no label of their own, so they are trusted only while the framework
    keeps handing an unlabelled item the invocation's label. A core that stopped would hide
    the verdict, and this is what says so at upgrade time.
    """

    def _through_middleware(self, answer):
        tool = _attach(answer, guidance=(_GUIDANCE,))
        middleware = LabelTrackingFunctionMiddleware()
        context = FunctionInvocationContext(function=tool, arguments={})

        async def call_next() -> None:
            context.result = await tool.invoke(arguments={})

        asyncio.run(middleware.process(context, call_next))
        return context.result, middleware

    def test_the_verdict_stays_readable_while_the_derived_half_is_hidden(self):
        answer = SandboxResult(
            completed=True, verdict="invalid", established=("e",), derived=("SECRET GUEST TEXT",)
        )
        items, middleware = self._through_middleware(answer)
        visible = [item.text for item in items if not self._hidden(item)]
        assert COMPLETED_TEXT in visible
        assert "Result: invalid" in visible
        assert "e" in visible
        assert _GUIDANCE in visible
        assert not any("SECRET GUEST TEXT" in text for text in visible)
        assert middleware.get_context_label().integrity.value == "trusted"

    @staticmethod
    def _hidden(item: Content) -> bool:
        return bool((item.additional_properties or {}).get("_variable_reference"))


class TestWhatIsRefused:
    """Every way the contract can be misused, named without quoting guest content."""

    def test_a_verdict_the_tool_never_declared(self):
        tool = _attach(SandboxResult(completed=True, verdict="maybe"))
        with pytest.raises(ValueError, match="did not declare"):
            _call(tool)

    def test_a_verdict_on_a_run_that_reached_none(self):
        tool = _attach(SandboxResult(completed=False, verdict="valid"))
        with pytest.raises(ValueError, match="no verdict to report"):
            _call(tool)

    def test_a_part_that_is_not_text(self):
        tool = _attach(SandboxResult(completed=True, derived=(Content.from_text("x"),)))
        with pytest.raises(ValueError, match=r"derived\[0\] is a "):
            _call(tool)

    def test_text_from_a_body_that_declared_the_contract(self):
        tool = _attach("plain text")
        with pytest.raises(ValueError, match="answered with a str"):
            _call(tool)

    def test_a_result_from_a_body_that_did_not_declare_the_contract(self):
        tool = _attach(SandboxResult(completed=True), contract=False, verdicts=())
        with pytest.raises(ValueError, match="does not declare result_contract"):
            _call(tool)

    def test_verdicts_declared_without_the_contract_that_reads_them(self):
        with pytest.raises(ValueError, match="nothing reads them"):
            _attach("text", contract=False)

    def test_the_contract_without_an_integrity_declaration(self):
        with pytest.raises(ValueError, match="declares no source_integrity"):
            _attach(SandboxResult(completed=True), source=None)

    @pytest.mark.parametrize("bad", [(object(),), (b"valid",), (1.5,)])
    def test_a_verdict_value_of_a_type_a_model_cannot_read(self, bad):
        with pytest.raises(ValueError, match="must be a str, an int or a bool"):
            _attach(SandboxResult(completed=True), verdicts=bad)

    def test_an_empty_verdict_value(self):
        with pytest.raises(ValueError, match="names nothing"):
            _attach(SandboxResult(completed=True), verdicts=("valid", "  "))

    def test_two_verdicts_a_model_would_read_as_one(self):
        with pytest.raises(ValueError, match="the same '1'"):
            _attach(SandboxResult(completed=True), verdicts=(1, "1"))

    def test_a_bool_does_not_pass_as_the_int_it_equals(self):
        """`False == 0` in Python, so equality alone would accept a value never declared."""
        tool = _attach(SandboxResult(completed=True, verdict=False), verdicts=(0, 1))
        with pytest.raises(ValueError, match="did not declare"):
            _call(tool)
