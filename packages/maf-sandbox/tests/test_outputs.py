"""Tests for `maf_sandbox._outputs` — the sink half of FILES_OUT.

Two properties here are the whole point of the module and are pinned from several directions:
nothing reaches the host's sink until the entire collection has been stat-ed, capped and read,
and a landing name is refused by the narrow invariant before either half of it can be seen.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    MAX_ARTIFACT_NAME_BYTES,
    Artifact,
    DeclaredOutput,
    EntryKind,
    LandedArtifact,
    NameNormalization,
    OutputDisposition,
    OutputSink,
    SandboxArtifactNameCollision,
    SandboxArtifactNameInvalid,
    SandboxEntry,
    SandboxOutputError,
    SandboxOutputMissing,
    SandboxOutputNotRegular,
    SandboxOutputSinkRequired,
    SandboxOutputSizeUnknown,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
    portable_name,
    validate_artifact_name,
)
from maf_sandbox.testing import InProcessSandbox

_WORK_DIR = "/work"
_KIND = "diagram-generator"
_PNG = "image/png"

#: One name in two Unicode forms — `e` plus a combining acute, and the precomposed `é`. Two
#: distinct `str` values, two distinct keys on Linux, one file on the other two.
_DECOMPOSED = "café.png"
_COMPOSED = "café.png"


def _spec(
    *outputs: DeclaredOutput, files_out: TransferLimits = DEFAULT_TRANSFER_LIMITS
) -> SandboxSpec:
    return SandboxSpec(
        kind=_KIND, work_dir=_WORK_DIR, declared_outputs=outputs, files_out=files_out
    )


class _RecordingSink:
    """A host sink that records what it was handed and answers with a reference.

    `fail_on` is the index of the delivery that raises, for the two halves of the
    no-partial-delivery rule: what the library can guarantee, and what it cannot.
    """

    def __init__(
        self,
        *,
        normalization: NameNormalization = NameNormalization.NFC,
        fail_on: int | None = None,
        log: list[tuple[str, str]] | None = None,
    ) -> None:
        self.delivered: list[Artifact] = []
        self.sink = OutputSink(deliver=self.deliver, normalization=normalization)
        self._fail_on = fail_on
        self._log = log

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        if self._log is not None:
            self._log.append(("deliver", artifact.name))
        if self._fail_on is not None and len(self.delivered) == self._fail_on:
            raise RuntimeError("the store refused")
        self.delivered.append(artifact)
        return LandedArtifact(
            name=artifact.name,
            display=f"[{artifact.name}]",
            handle=f"blob://{artifact.name}?sig=secret",
        )

    @property
    def names(self) -> list[str]:
        return [artifact.name for artifact in self.delivered]


class _RecordingSandbox(InProcessSandbox):
    """An `InProcessSandbox` that logs the order of the pull calls made against it."""

    def __init__(self, *, log: list[tuple[str, str]] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls = log if log is not None else []

    async def stat_file(self, path, *, working_directory):
        self.calls.append(("stat", path))
        return await super().stat_file(path, working_directory=working_directory)

    async def read_file(self, path, *, working_directory):
        self.calls.append(("read", path))
        return await super().read_file(path, working_directory=working_directory)


class _StubSandbox:
    """The two pull calls `collect_outputs` makes, scripted directly.

    `InProcessSandbox` is honest, so it cannot express either of the two things a real guest
    can do to a reader: report a regular file whose size is unknown, and grow a file between
    the stat and the read.
    """

    def __init__(
        self, entries: dict[str, SandboxEntry], contents: dict[str, bytes] | None = None
    ) -> None:
        self.entries = entries
        self.contents = contents or {}

    async def stat_file(self, path, *, working_directory):
        return self.entries.get(path)

    async def read_file(self, path, *, working_directory):
        return self.contents[path]


class TestArtifactShapes:
    def test_artifact_is_frozen(self):
        artifact = Artifact(name="a.png", content=b"x", kind=_KIND, media_type=_PNG)
        with pytest.raises(dataclasses.FrozenInstanceError):
            artifact.name = "b.png"  # type: ignore[misc]

    def test_landed_artifact_is_frozen(self):
        landed = LandedArtifact(name="a.png", display="a.png")
        with pytest.raises(dataclasses.FrozenInstanceError):
            landed.display = "other"  # type: ignore[misc]

    def test_a_landed_artifact_need_not_carry_a_handle(self):
        assert LandedArtifact(name="a.png", display="a.png").handle is None

    def test_output_sink_is_frozen(self):
        sink = _RecordingSink().sink
        with pytest.raises(dataclasses.FrozenInstanceError):
            sink.normalization = NameNormalization.NONE  # type: ignore[misc]

    def test_normalization_defaults_to_nfc(self):
        async def deliver(artifact: Artifact) -> LandedArtifact:
            return LandedArtifact(name=artifact.name, display=artifact.name)

        assert OutputSink(deliver=deliver).normalization == NameNormalization.NFC

    def test_a_sink_carries_no_confidentiality_cap(self):
        """One value from one source: the host's outbound cap, supplied where the tool is
        built. A second one here would exist only to be folded with the first, and the value
        is an opaque host-vocabulary string that nothing in a library can rank."""
        assert {field.name for field in dataclasses.fields(OutputSink)} == {
            "deliver",
            "normalization",
        }


class TestValidateArtifactName:
    @pytest.mark.parametrize(
        "name",
        ["diagram.png", "out/diagram.png", "out/nested/diagram.png", "café.png", "a" * 255],
    )
    def test_accepts_a_relative_bounded_utf8_name(self, name: str):
        assert validate_artifact_name(name) is None

    @pytest.mark.parametrize(
        ("name", "rule"),
        [
            ("", "non-empty"),
            ("out\\diagram.png", "backslash"),
            ("/etc/passwd", "absolute"),
            ("../escape.png", "traversal"),
            ("out/../../escape.png", "traversal"),
            ("\udcff.png", "not valid UTF-8"),
            ("a" * 256, "ceiling"),
        ],
    )
    def test_each_rule_refuses_by_name(self, name: str, rule: str):
        with pytest.raises(SandboxArtifactNameInvalid, match=rule):
            validate_artifact_name(name)

    def test_the_length_bound_counts_utf8_bytes_not_characters(self):
        """`é` is one character and two bytes, and the destinations that impose a limit count
        bytes."""
        name = "é" * MAX_ARTIFACT_NAME_BYTES
        with pytest.raises(SandboxArtifactNameInvalid, match="ceiling"):
            validate_artifact_name(name)

    def test_it_does_not_reach_for_the_destination_rules(self):
        """`CON` is unusable on Windows and perfectly fine in a blob container — which is why
        `portable_name` is a separate, opt-in helper."""
        assert validate_artifact_name("CON") is None


class TestPortableName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("CON", "CON_"),
            ("con", "con_"),
            ("CON.txt", "CON_.txt"),
            ("PRN", "PRN_"),
            ("prn.ps", "prn_.ps"),
            ("AUX", "AUX_"),
            ("aux.txt", "aux_.txt"),
            ("NUL", "NUL_"),
            ("nul.log", "nul_.log"),
            ("COM1", "COM1_"),
            ("COM9.dat", "COM9_.dat"),
            ("LPT1", "LPT1_"),
            ("LPT9", "LPT9_"),
        ],
    )
    def test_a_reserved_device_name_is_rewritten_with_or_without_an_extension(
        self, name: str, expected: str
    ):
        assert portable_name(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("COM¹", "COM¹_"),
            ("COM²", "COM²_"),
            ("COM³", "COM³_"),
            ("com¹.log", "com¹_.log"),
            ("LPT¹", "LPT¹_"),
            ("LPT²", "LPT²_"),
            ("LPT³", "LPT³_"),
        ],
    )
    def test_a_superscript_port_number_is_reserved_too(self, name: str, expected: str):
        """Not a typo, and the entry a reader is most likely to delete as one: Windows reads the
        ISO/IEC 8859-1 superscript digits as digits, so `echo test > COM¹` fails to create a
        file exactly as `COM1` does. Microsoft's naming rules list all six."""
        assert portable_name(name) == expected

    @pytest.mark.parametrize(
        "name",
        ["COM0", "COM10", "COM⁴", "console.txt", "auxiliary.log", "a.CON", "contract.pdf"],
    )
    def test_a_name_that_merely_resembles_a_device_is_left_alone(self, name: str):
        """Nothing beyond the authoritative list: `COM⁴` is a legitimate name — Windows treats
        only ¹, ² and ³ as digits — and a helper that guessed would mangle it."""
        assert portable_name(name) == name

    def test_the_forbidden_set_is_replaced(self):
        assert portable_name('a<b>c:d"e|f?g*h.png') == "a_b_c_d_e_f_g_h.png"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("report.", "report"), ("report ", "report"), ("report. .", "report")],
    )
    def test_trailing_dots_and_spaces_are_stripped(self, name: str, expected: str):
        assert portable_name(name) == expected

    def test_every_segment_is_treated_as_a_name(self):
        assert portable_name("out/CON.txt") == "out/CON_.txt"

    def test_a_segment_left_empty_becomes_the_replacement(self):
        assert portable_name("...") == "_"

    def test_an_ordinary_name_survives_unchanged(self):
        assert portable_name("out/diagram.png") == "out/diagram.png"

    def test_it_is_never_applied_for_you(self):
        """The library's own invariant accepts `CON`; only a host that asks gets it rewritten."""
        sandbox = InProcessSandbox(seed_files={"/work/CON": b"x"})
        recorder = _RecordingSink()
        asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="CON")), sink=recorder.sink))
        assert recorder.names == ["CON"]


class TestCollectOutputs:
    def test_lands_every_declared_output_in_declaration_order(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"aa", "/work/b.png": b"bbb"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="b.png"), DeclaredOutput(path="a.png")),
                sink=recorder.sink,
            )
        )
        assert [item.name for item in landed] == ["b.png", "a.png"]
        assert recorder.names == ["b.png", "a.png"]

    def test_the_artifact_carries_the_spec_kind_and_the_declared_media_type(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"\x89PNG"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="a.png", media_type=_PNG)),
                sink=recorder.sink,
            )
        )
        (artifact,) = recorder.delivered
        assert (artifact.kind, artifact.media_type, artifact.content) == (_KIND, _PNG, b"\x89PNG")

    def test_a_declared_output_keeps_its_own_separators(self):
        sandbox = InProcessSandbox(seed_files={"/work/out/diagram.png": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(
                sandbox, _spec(DeclaredOutput(path="out/diagram.png")), sink=recorder.sink
            )
        )
        assert recorder.names == ["out/diagram.png"]

    def test_what_the_host_returned_is_what_the_caller_gets(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
        )
        assert landed == (
            LandedArtifact(name="a.png", display="[a.png]", handle="blob://a.png?sig=secret"),
        )

    def test_a_spec_declaring_nothing_collects_nothing_and_needs_no_sink(self):
        assert asyncio.run(collect_outputs(InProcessSandbox(), _spec())) == ()

    def test_the_result_is_a_tuple(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
        )
        assert isinstance(landed, tuple)


class TestCollectionOrderIsNormative:
    def test_everything_is_stat_ed_and_read_before_anything_is_delivered(self):
        """The property the no-partial-delivery rule rests on: by the time the host is handed
        its first artifact, every refusal this module can raise has already been settled."""
        log: list[tuple[str, str]] = []
        sandbox = _RecordingSandbox(
            log=log, seed_files={"/work/a.png": b"aa", "/work/b.png": b"bb"}
        )
        recorder = _RecordingSink(log=log)
        asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="a.png"), DeclaredOutput(path="b.png")),
                sink=recorder.sink,
            )
        )
        assert log == [
            ("stat", "a.png"),
            ("stat", "b.png"),
            ("read", "a.png"),
            ("read", "b.png"),
            ("deliver", "a.png"),
            ("deliver", "b.png"),
        ]


class TestDisposition:
    def test_a_consume_output_never_reaches_the_sink_and_is_never_read(self):
        """Its bytes are the kind's own `read_file` call — a source, answering to integrity,
        not a sink answering to confidentiality."""
        sandbox = _RecordingSandbox(seed_files={"/work/result.sarif": b"{}", "/work/a.png": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(
                    DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME),
                    DeclaredOutput(path="a.png"),
                ),
                sink=recorder.sink,
            )
        )
        assert [item.name for item in landed] == ["a.png"]
        assert ("read", "result.sarif") not in sandbox.calls

    def test_a_consume_only_spec_needs_no_sink(self):
        sandbox = InProcessSandbox(seed_files={"/work/result.sarif": b"{}"})
        spec = _spec(DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME))
        assert asyncio.run(collect_outputs(sandbox, spec)) == ()

    def test_required_means_the_same_thing_for_a_consume_output(self):
        spec = _spec(DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME))
        with pytest.raises(SandboxOutputMissing, match="result.sarif"):
            asyncio.run(collect_outputs(InProcessSandbox(), spec))


class TestPresence:
    def test_a_missing_required_output_is_an_error_naming_the_file(self):
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputMissing, match="diagram.png"):
            asyncio.run(
                collect_outputs(
                    InProcessSandbox(),
                    _spec(DeclaredOutput(path="diagram.png")),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_a_missing_optional_output_is_simply_absent(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(
                    DeclaredOutput(path="missing.png", required=False),
                    DeclaredOutput(path="a.png"),
                ),
                sink=recorder.sink,
            )
        )
        assert [item.name for item in landed] == ["a.png"]

    def test_every_declared_output_absent_lands_nothing_rather_than_failing(self):
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                InProcessSandbox(),
                _spec(DeclaredOutput(path="a.png", required=False)),
                sink=recorder.sink,
            )
        )
        assert landed == ()
        assert recorder.delivered == []


class TestEntryKindAndSize:
    def test_a_non_regular_entry_is_refused(self):
        sandbox = InProcessSandbox(seed_files={"/work/out.png": EntryKind.OTHER})
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputNotRegular, match="out.png"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="out.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []

    def test_a_directory_is_refused(self):
        sandbox = InProcessSandbox(seed_files={"/work/out/a.png": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputNotRegular, match="directory"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="out")), sink=recorder.sink)
            )

    def test_an_undeterminable_size_fails_closed(self):
        """Coercing an unknown size to zero would make every cap read the one file it cannot
        measure as free."""
        sandbox = _StubSandbox(
            entries={"a.png": SandboxEntry(path="a.png", kind=EntryKind.FILE, size_bytes=None)},
            contents={"a.png": b"x"},
        )
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputSizeUnknown, match="a.png"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []


class TestCaps:
    def test_max_bytes_per_file_names_the_cap_and_the_file(self):
        sandbox = InProcessSandbox(seed_files={"/work/big.png": b"0123456789"})
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=4, max_total_bytes=100, max_files=10)
        with pytest.raises(SandboxTransferCapExceeded, match="max_bytes_per_file.*4"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="big.png"), files_out=limits),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_max_files_names_the_cap_and_the_file(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"a", "/work/b.png": b"b"})
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=100, max_total_bytes=100, max_files=1)
        with pytest.raises(SandboxTransferCapExceeded, match="b.png.*max_files=1"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="a.png"),
                        DeclaredOutput(path="b.png"),
                        files_out=limits,
                    ),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_max_total_bytes_bounds_the_collection_not_the_file(self):
        """Ten thousand files one byte under the per-file ceiling cost exactly what the
        ceiling was written to prevent."""
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"aaa", "/work/b.png": b"bbb"})
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=100, max_total_bytes=5, max_files=10)
        with pytest.raises(SandboxTransferCapExceeded, match="max_total_bytes"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="a.png"),
                        DeclaredOutput(path="b.png"),
                        files_out=limits,
                    ),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_a_cap_breach_leaves_nothing_landed_even_when_the_first_file_fits(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"a", "/work/big.png": b"0123456"})
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=3, max_total_bytes=100, max_files=10)
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="a.png"),
                        DeclaredOutput(path="big.png"),
                        files_out=limits,
                    ),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_a_file_that_grows_between_the_stat_and_the_read_is_still_refused(self):
        """A stat is a promise about a file the guest is free to rewrite; only the length
        actually read bounds what reaches the host."""
        sandbox = _StubSandbox(
            entries={"a.png": SandboxEntry(path="a.png", kind=EntryKind.FILE, size_bytes=2)},
            contents={"a.png": b"0123456789"},
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=4, max_total_bytes=100, max_files=10)
        with pytest.raises(SandboxTransferCapExceeded, match="max_bytes_per_file"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a.png"), files_out=limits),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []


class TestNames:
    def test_a_landing_name_is_held_to_the_narrow_invariant(self):
        """Settled from the spec alone, so the refusal does not depend on what the guest
        happened to produce — the sandbox is never touched."""
        sandbox = _RecordingSandbox(seed_files={"/work/a.png": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameInvalid, match="traversal"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="../a.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []
        assert sandbox.calls == []

    def test_names_are_normalized_to_nfc_before_deliver_sees_them(self):
        sandbox = InProcessSandbox(seed_files={f"/work/{_DECOMPOSED}": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path=_DECOMPOSED)), sink=recorder.sink)
        )
        assert recorder.names == [_COMPOSED]

    def test_none_writes_what_was_asked_for_byte_exactly(self):
        sandbox = InProcessSandbox(seed_files={f"/work/{_DECOMPOSED}": b"x"})
        recorder = _RecordingSink(normalization=NameNormalization.NONE)
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path=_DECOMPOSED)), sink=recorder.sink)
        )
        assert recorder.names == [_DECOMPOSED]

    def test_a_case_only_collision_is_refused_across_one_collection(self):
        """Two files on Linux and one on Windows and default macOS — and the host, handed
        artifacts one at a time, could never see it."""
        sandbox = InProcessSandbox(
            seed_files={"/work/Diagram.png": b"a", "/work/diagram.png": b"b"}
        )
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameCollision, match="Diagram.png"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="Diagram.png"), DeclaredOutput(path="diagram.png")),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_collisions_compare_normalized_forms_even_when_normalization_is_off(self):
        """Opting out disables the rewrite and nothing else: compare normalized, write what
        was asked for."""
        sandbox = InProcessSandbox(
            seed_files={f"/work/{_DECOMPOSED}": b"a", f"/work/{_COMPOSED}": b"b"}
        )
        recorder = _RecordingSink(normalization=NameNormalization.NONE)
        with pytest.raises(SandboxArtifactNameCollision):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path=_DECOMPOSED), DeclaredOutput(path=_COMPOSED)),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []


class TestSinkIsRequiredForAnythingThatLands:
    def test_a_landing_output_without_a_sink_is_refused(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"x"})
        with pytest.raises(SandboxOutputSinkRequired, match="a.png"):
            asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png"))))

    def test_it_is_refused_before_the_sandbox_is_touched(self):
        sandbox = _RecordingSandbox(seed_files={"/work/a.png": b"x"})
        with pytest.raises(SandboxOutputSinkRequired):
            asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png"))))
        assert sandbox.calls == []


class TestDeliveryFailure:
    def test_a_deliver_that_raises_on_the_first_artifact_lands_nothing(self):
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"a", "/work/b.png": b"b"})
        recorder = _RecordingSink(fail_on=0)
        with pytest.raises(RuntimeError, match="the store refused"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a.png"), DeclaredOutput(path="b.png")),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_a_deliver_that_raises_part_way_keeps_what_it_already_accepted(self):
        """The one residue the library cannot cover, pinned so it is a known property rather
        than a surprise: a push callback cannot be un-called, which is why every check that
        can be made is made before the first delivery."""
        sandbox = InProcessSandbox(seed_files={"/work/a.png": b"a", "/work/b.png": b"b"})
        recorder = _RecordingSink(fail_on=1)
        with pytest.raises(RuntimeError, match="the store refused"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a.png"), DeclaredOutput(path="b.png")),
                    sink=recorder.sink,
                )
            )
        assert recorder.names == ["a.png"]


class TestRefusalsShareOneBase:
    @pytest.mark.parametrize(
        "error",
        [
            SandboxOutputMissing,
            SandboxOutputNotRegular,
            SandboxOutputSizeUnknown,
            SandboxTransferCapExceeded,
            SandboxOutputSinkRequired,
            SandboxArtifactNameInvalid,
            SandboxArtifactNameCollision,
        ],
    )
    def test_every_refusal_is_a_sandbox_output_error(self, error: type[Exception]):
        """So a kind that only needs to tell the model "the artifacts did not come back" can
        catch one thing."""
        assert issubclass(error, SandboxOutputError)
