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
    SandboxLandingNotConfined,
    SandboxOutputError,
    SandboxOutputMissing,
    SandboxOutputNotConfined,
    SandboxOutputNotRegular,
    SandboxOutputSinkRequired,
    SandboxOutputSizeUnknown,
    SandboxOutputUnreachable,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
    make_file_system_sink,
    portable_file_name,
    validate_artifact_name,
)
from maf_sandbox.testing import InProcessSandbox

_WORK_DIR = "/maf-sandbox/work"
_KIND = "diagram-generator"
_PNG = "image/png"

#: One name in two Unicode forms — `e` plus a combining acute, and the precomposed `é`. Two
#: distinct `str` values, two distinct keys on Linux, one file on the other two.
_DECOMPOSED = "café.png"
_COMPOSED = "café.png"

#: U+0958 is three UTF-8 bytes and a composition exclusion, so NFC *decomposes* it into two
#: characters of three bytes each: 85 of them pass a 255-byte check and are delivered at 510.
_NFC_GROWS = "क़" * 85


def _spec(
    *outputs: DeclaredOutput,
    files_out: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    at_call_time: bool = False,
) -> SandboxSpec:
    return SandboxSpec(
        kind=_KIND,
        work_dir=_WORK_DIR,
        declared_outputs=outputs,
        outputs_named_at_call_time=at_call_time,
        files_out=files_out,
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

    async def read_file(self, path, *, working_directory, max_bytes):
        self.calls.append(("read", path))
        return await super().read_file(
            path, working_directory=working_directory, max_bytes=max_bytes
        )


class _StubSandbox:
    """The two pull calls `collect_outputs` makes, scripted directly.

    `InProcessSandbox` is honest, so it cannot express either of the two things a real guest
    can do to a reader: report a regular file whose size is unknown, and grow a file between
    the stat and the read. It ignores `max_bytes` for the same reason a backend whose SDK
    buffers the whole response internally has to — which is what makes it the fixture that
    proves the caller's own re-count still catches the growth.
    """

    def __init__(
        self, entries: dict[str, SandboxEntry], contents: dict[str, bytes] | None = None
    ) -> None:
        self.entries = entries
        self.contents = contents or {}
        self.budgets: list[int] = []

    async def stat_file(self, path, *, working_directory):
        return self.entries.get(path)

    async def read_file(self, path, *, working_directory, max_bytes):
        self.budgets.append(max_bytes)
        return self.contents[path]

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Not what this double is for; the protocol needs it present, not useful."""
        raise NotImplementedError


class _RaisingSandbox:
    """A backend that answers the pull surface in its own vocabulary, as a real one does."""

    def __init__(self, error: BaseException, *, on_read: bool = False) -> None:
        self._error = error
        self._on_read = on_read

    async def stat_file(self, path, *, working_directory):
        if self._on_read:
            return SandboxEntry(path=path, kind=EntryKind.FILE, size_bytes=1)
        raise self._error

    async def read_file(self, path, *, working_directory, max_bytes):
        raise self._error

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Not what this double is for; the protocol needs it present, not useful."""
        raise NotImplementedError


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
            ("a//b", "segment"),
            ("a/./b", "segment"),
            ("a\x00b.png", "control character"),
            ("a\nb.png", "control character"),
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

    @pytest.mark.parametrize("name", ["a//b", "a/./b"])
    def test_a_segment_that_names_nothing_is_refused(self, name: str):
        """`a/b`, `a//b` and `a/./b` are one file to every filesystem, so delivering them as
        written would land three artifacts for it."""
        with pytest.raises(SandboxArtifactNameInvalid, match="segment"):
            validate_artifact_name(name)

    @pytest.mark.parametrize("name", ["a\x00b.png", "a\nb.png"])
    def test_a_control_character_is_refused(self, name: str):
        """NUL and newline: no filesystem accepts either, which is what keeps this inside the
        narrow invariant rather than a guess about the destination's own namespace."""
        with pytest.raises(SandboxArtifactNameInvalid, match="control character"):
            validate_artifact_name(name)

    def test_it_does_not_reach_for_the_destination_rules(self):
        """`CON` is unusable on Windows and perfectly fine in a blob container — which is why
        `portable_file_name` is a separate, opt-in helper."""
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
        assert portable_file_name(name) == expected

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
        assert portable_file_name(name) == expected

    @pytest.mark.parametrize(
        "name",
        ["COM0", "COM10", "COM⁴", "console.txt", "auxiliary.log", "a.CON", "contract.pdf"],
    )
    def test_a_name_that_merely_resembles_a_device_is_left_alone(self, name: str):
        """Nothing beyond the authoritative list: `COM⁴` is a legitimate name — Windows treats
        only ¹, ² and ³ as digits — and a helper that guessed would mangle it."""
        assert portable_file_name(name) == name

    def test_the_forbidden_set_is_replaced(self):
        assert portable_file_name('a<b>c:d"e|f?g*h.png') == "a_b_c_d_e_f_g_h.png"

    def test_ascii_control_characters_are_replaced_too(self):
        """Microsoft's rules list ASCII 0-31 in the same breath as the punctuation above, so a
        helper covering only the visible half hands Windows a name it still refuses."""
        assert portable_file_name("a\x00b\x1fc\nd.png") == "a_b_c_d.png"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("report.", "report"), ("report ", "report"), ("report. .", "report")],
    )
    def test_trailing_dots_and_spaces_are_stripped(self, name: str, expected: str):
        assert portable_file_name(name) == expected

    def test_every_segment_is_treated_as_a_name(self):
        assert portable_file_name("out/CON.txt") == "out/CON_.txt"

    def test_a_segment_left_empty_becomes_the_replacement(self):
        assert portable_file_name("...") == "_"

    def test_an_ordinary_name_survives_unchanged(self):
        assert portable_file_name("out/diagram.png") == "out/diagram.png"

    def test_it_is_never_applied_for_you(self):
        """The library's own invariant accepts `CON`; only a host that asks gets it rewritten."""
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/CON": b"x"})
        recorder = _RecordingSink()
        asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="CON")), sink=recorder.sink))
        assert recorder.names == ["CON"]


class TestCollectOutputs:
    def test_lands_every_declared_output_in_declaration_order(self):
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"aa", "/maf-sandbox/work/b.png": b"bbb"}
        )
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"\x89PNG"})
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/out/diagram.png": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(
                sandbox, _spec(DeclaredOutput(path="out/diagram.png")), sink=recorder.sink
            )
        )
        assert recorder.names == ["out/diagram.png"]

    def test_what_the_host_returned_is_what_the_caller_gets(self):
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
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
            log=log, seed_files={"/maf-sandbox/work/a.png": b"aa", "/maf-sandbox/work/b.png": b"bb"}
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
        sandbox = _RecordingSandbox(
            seed_files={"/maf-sandbox/work/result.sarif": b"{}", "/maf-sandbox/work/a.png": b"x"}
        )
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/result.sarif": b"{}"})
        spec = _spec(DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME))
        assert asyncio.run(collect_outputs(sandbox, spec)) == ()

    def test_required_means_the_same_thing_for_a_consume_output(self):
        spec = _spec(DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME))
        with pytest.raises(SandboxOutputMissing, match="result.sarif"):
            asyncio.run(collect_outputs(InProcessSandbox(), spec))

    def test_a_consume_output_counts_against_max_files(self):
        """`files_out` bounds the collection the spec declared, not the subset that lands: a
        kind that reads its own outputs still moves those bytes out of the sandbox."""
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/result.sarif": b"{}", "/maf-sandbox/work/a.png": b"x"}
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=100, max_total_bytes=100, max_files=1)
        with pytest.raises(SandboxTransferCapExceeded, match="max_files=1"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME),
                        DeclaredOutput(path="a.png"),
                        files_out=limits,
                    ),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_a_consume_output_counts_against_max_total_bytes(self):
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/result.sarif": b"{}{}", "/maf-sandbox/work/a.png": b"xx"}
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=100, max_total_bytes=5, max_files=10)
        with pytest.raises(SandboxTransferCapExceeded, match="max_total_bytes"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME),
                        DeclaredOutput(path="a.png"),
                        files_out=limits,
                    ),
                    sink=recorder.sink,
                )
            )

    def test_a_consume_output_of_unknown_size_fails_closed_too(self):
        sandbox = _StubSandbox(
            entries={
                "result.sarif": SandboxEntry(
                    path="result.sarif", kind=EntryKind.FILE, size_bytes=None
                )
            }
        )
        spec = _spec(DeclaredOutput(path="result.sarif", disposition=OutputDisposition.CONSUME))
        with pytest.raises(SandboxOutputSizeUnknown, match="result.sarif"):
            asyncio.run(collect_outputs(sandbox, spec))

    def test_a_consume_path_is_held_to_the_narrow_invariant_as_well(self):
        """It is still a path this library hands to a backend. Unvalidated, the refusal would
        come back as that backend's own exception rather than as one a kind can catch."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        spec = _spec(DeclaredOutput(path="../escape.sarif", disposition=OutputDisposition.CONSUME))
        with pytest.raises(SandboxArtifactNameInvalid, match="traversal"):
            asyncio.run(collect_outputs(sandbox, spec))
        assert sandbox.calls == []


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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/out.png": EntryKind.OTHER})
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputNotRegular, match="out.png"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="out.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []

    def test_a_directory_is_refused(self):
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/out/a.png": b"x"})
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/big.png": b"0123456789"})
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
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"a", "/maf-sandbox/work/b.png": b"b"}
        )
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
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"aaa", "/maf-sandbox/work/b.png": b"bbb"}
        )
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
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"a", "/maf-sandbox/work/big.png": b"0123456"}
        )
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


class _ShrinkingStatSandbox(InProcessSandbox):
    """Stats every file as a single byte, so any real read is over the bound it was given.

    The only way to exercise the fake's own refusal end to end: `InProcessSandbox` is honest,
    and an honest stat is never smaller than the file it describes.
    """

    async def stat_file(self, path, *, working_directory):
        entry = await super().stat_file(path, working_directory=working_directory)
        return entry if entry is None else dataclasses.replace(entry, size_bytes=1)


class TestTheReadBudget:
    """`max_total_bytes` is a memory bound only if it reaches the read, not just the count.

    Reading first and capping afterwards makes the cap a statement about what is *delivered*
    and nothing at all about what was buffered to decide it.
    """

    def test_a_read_is_bounded_by_the_stat_ed_size(self):
        sandbox = _StubSandbox(
            entries={"a.png": SandboxEntry(path="a.png", kind=EntryKind.FILE, size_bytes=3)},
            contents={"a.png": b"abc"},
        )
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
        )
        assert sandbox.budgets == [3]

    def test_the_bound_is_clamped_by_what_the_collection_has_left(self):
        """The first file grew after its stat, so the second one may only read the remainder —
        the stat-ed size alone would let a collection buffer past its own total."""
        sandbox = _StubSandbox(
            entries={
                "a.png": SandboxEntry(path="a.png", kind=EntryKind.FILE, size_bytes=5),
                "b.png": SandboxEntry(path="b.png", kind=EntryKind.FILE, size_bytes=5),
            },
            contents={"a.png": b"a" * 8, "b.png": b"bb"},
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=8, max_total_bytes=10, max_files=10)
        asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="a.png"), DeclaredOutput(path="b.png"), files_out=limits),
                sink=recorder.sink,
            )
        )
        assert sandbox.budgets == [5, 2]

    def test_a_backend_that_can_enforce_the_bound_refuses_rather_than_truncating(self):
        """Half a PNG returned as success is an artifact the host cannot tell from a whole
        one, so the fake — like the protocol — refuses instead."""
        sandbox = _ShrinkingStatSandbox(seed_files={"/maf-sandbox/work/a.png": b"0123"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxTransferCapExceeded, match="a.png"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []


class TestBackendFailuresJoinTheFamily:
    """A backend answers in its own vocabulary; a kind is told to catch one base class.

    Both of these used to escape it — the first as a bare `ValueError` and the second as a
    bare `FileNotFoundError` — so "catch `SandboxOutputError`" was advice that did not hold.
    """

    def _collect(self, sandbox) -> None:
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
        )

    def test_a_path_the_backend_resolves_outside_the_working_directory(self):
        with pytest.raises(SandboxOutputNotConfined, match="a.png"):
            self._collect(_RaisingSandbox(ValueError("resolves outside /maf-sandbox/work")))

    def test_a_file_that_went_away_between_the_stat_and_the_read(self):
        with pytest.raises(SandboxOutputUnreachable, match="a.png"):
            self._collect(_RaisingSandbox(FileNotFoundError("no such file"), on_read=True))

    def test_the_backends_own_error_survives_as_the_cause(self):
        """Translated for the caller, not swallowed: the provider's text is still in the
        traceback for whoever is debugging the backend."""
        original = FileNotFoundError("no such file")
        with pytest.raises(SandboxOutputUnreachable) as caught:
            self._collect(_RaisingSandbox(original, on_read=True))
        assert caught.value.__cause__ is original


class TestNames:
    def test_a_landing_name_is_held_to_the_narrow_invariant(self):
        """Settled from the spec alone, so the refusal does not depend on what the guest
        happened to produce — the sandbox is never touched."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameInvalid, match="traversal"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path="../a.png")), sink=recorder.sink)
            )
        assert recorder.delivered == []
        assert sandbox.calls == []

    def test_names_are_normalized_to_nfc_before_deliver_sees_them(self):
        sandbox = InProcessSandbox(seed_files={f"/maf-sandbox/work/{_DECOMPOSED}": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path=_DECOMPOSED)), sink=recorder.sink)
        )
        assert recorder.names == [_COMPOSED]

    def test_none_writes_what_was_asked_for_byte_exactly(self):
        sandbox = InProcessSandbox(seed_files={f"/maf-sandbox/work/{_DECOMPOSED}": b"x"})
        recorder = _RecordingSink(normalization=NameNormalization.NONE)
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path=_DECOMPOSED)), sink=recorder.sink)
        )
        assert recorder.names == [_DECOMPOSED]

    def test_a_case_only_collision_is_refused_across_one_collection(self):
        """Two files on Linux and one on Windows and default macOS — and the host, handed
        artifacts one at a time, could never see it."""
        sandbox = InProcessSandbox(
            seed_files={
                "/maf-sandbox/work/Diagram.png": b"a",
                "/maf-sandbox/work/diagram.png": b"b",
            }
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

    @pytest.mark.parametrize(
        ("first", "second"),
        [("Straße.png", "Strasse.png"), ("ﬁle.png", "file.png")],
    )
    def test_two_names_every_filesystem_keeps_apart_are_accepted(self, first: str, second: str):
        """`str.casefold` maps `ß` to `ss` and `ﬁ` (U+FB01) to `fi`, so folding would refuse
        these pairs — which are two distinct files on Linux, NTFS and case-insensitive APFS
        alike, and would fail the whole collection with a reason that is not true."""
        sandbox = InProcessSandbox(
            seed_files={f"/maf-sandbox/work/{first}": b"a", f"/maf-sandbox/work/{second}": b"b"}
        )
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path=first), DeclaredOutput(path=second)),
                sink=recorder.sink,
            )
        )
        assert [item.name for item in landed] == [first, second]

    @pytest.mark.parametrize("twin", ["a//b", "a/./b"])
    def test_two_spellings_of_one_path_cannot_both_be_delivered(self, twin: str):
        """`a/b`, `a//b` and `a/./b` are one file in the guest, and the collision check used
        to key on the raw string — so all three landed, as three artifacts."""
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a/b": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxOutputError):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a/b"), DeclaredOutput(path=twin)),
                    sink=recorder.sink,
                )
            )
        assert recorder.delivered == []

    def test_the_byte_ceiling_judges_the_name_that_is_actually_delivered(self):
        """NFC is not length-non-increasing: 85 × U+0958 is 255 bytes as declared and 510 as
        composed, so a check made before the rewrite is a check of a different name."""
        sandbox = InProcessSandbox(seed_files={f"/maf-sandbox/work/{_NFC_GROWS}": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameInvalid, match="ceiling"):
            asyncio.run(
                collect_outputs(sandbox, _spec(DeclaredOutput(path=_NFC_GROWS)), sink=recorder.sink)
            )
        assert recorder.delivered == []

    def test_the_same_name_is_accepted_by_a_sink_that_rewrites_nothing(self):
        """Which is the point: what is judged is the spelling the host is handed."""
        sandbox = InProcessSandbox(seed_files={f"/maf-sandbox/work/{_NFC_GROWS}": b"x"})
        recorder = _RecordingSink(normalization=NameNormalization.NONE)
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path=_NFC_GROWS)), sink=recorder.sink)
        )
        assert recorder.names == [_NFC_GROWS]

    def test_collisions_compare_normalized_forms_even_when_normalization_is_off(self):
        """Opting out disables the rewrite and nothing else: compare normalized, write what
        was asked for."""
        sandbox = InProcessSandbox(
            seed_files={
                f"/maf-sandbox/work/{_DECOMPOSED}": b"a",
                f"/maf-sandbox/work/{_COMPOSED}": b"b",
            }
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
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        with pytest.raises(SandboxOutputSinkRequired, match="a.png"):
            asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png"))))

    def test_it_is_refused_before_the_sandbox_is_touched(self):
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        with pytest.raises(SandboxOutputSinkRequired):
            asyncio.run(collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png"))))
        assert sandbox.calls == []

    def test_a_call_time_output_without_a_sink_is_refused_and_named(self):
        """By the time a collection runs the names exist, so the refusal states them — it is
        `sandboxed_tool`, refusing at attach, that has nothing yet to name."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        with pytest.raises(SandboxOutputSinkRequired, match="a.png"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(at_call_time=True),
                    outputs=(DeclaredOutput(path="a.png"),),
                )
            )
        assert sandbox.calls == []


class TestTheLandingNameIsNotAlwaysTheGuestPath:
    """A kind writing into a per-call directory must not land `<run-id>/report.csv`."""

    def test_the_declared_name_is_what_the_sink_receives(self):
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/run-1/report.csv": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="run-1/report.csv", name="report.csv")),
                sink=recorder.sink,
            )
        )
        assert recorder.names == ["report.csv"]
        assert [item.name for item in landed] == ["report.csv"]

    def test_the_guest_path_is_still_what_is_read(self):
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/run-1/report.csv": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="run-1/report.csv", name="report.csv")),
                sink=recorder.sink,
            )
        )
        assert sandbox.calls == [("stat", "run-1/report.csv"), ("read", "run-1/report.csv")]

    def test_it_defaults_to_the_path_so_every_kind_written_before_it_is_unchanged(self):
        assert DeclaredOutput(path="a.png").name is None
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(sandbox, _spec(DeclaredOutput(path="a.png")), sink=recorder.sink)
        )
        assert recorder.names == ["a.png"]

    def test_the_landing_name_is_held_to_the_narrow_invariant(self):
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/run-1/report.csv": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameInvalid, match="traversal"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="run-1/report.csv", name="../report.csv")),
                    sink=recorder.sink,
                )
            )
        assert sandbox.calls == []

    def test_the_guest_path_is_held_to_it_too(self):
        """Both halves cross a boundary: the path goes to a backend, the name to the host."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameInvalid, match="traversal"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="../a.png", name="a.png")),
                    sink=recorder.sink,
                )
            )
        assert sandbox.calls == []

    def test_two_run_directories_landing_one_name_collide(self):
        """The paths differ, so keying the collision check on them would have missed it."""
        sandbox = InProcessSandbox(
            seed_files={
                "/maf-sandbox/work/run-1/report.csv": b"a",
                "/maf-sandbox/work/run-2/report.csv": b"b",
            }
        )
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameCollision) as refusal:
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="run-1/report.csv", name="report.csv"),
                        DeclaredOutput(path="run-2/report.csv", name="report.csv"),
                    ),
                    sink=recorder.sink,
                )
            )
        # Both guest paths, because the landing name alone would print the same string twice.
        assert "run-1/report.csv" in str(refusal.value)
        assert "run-2/report.csv" in str(refusal.value)
        # And not the variant wording: these two names are identical, not case-folded twins.
        assert "differing only by case" not in str(refusal.value)
        assert recorder.delivered == []

    def test_a_case_only_collision_still_says_which_difference_it_is(self):
        """The other half: two names that really are variants keep the explanation that makes
        the refusal understandable, since nothing about them looks wrong on Linux."""
        sandbox = InProcessSandbox(
            seed_files={
                "/maf-sandbox/work/run-1/a.png": b"a",
                "/maf-sandbox/work/run-2/b.png": b"b",
            }
        )
        recorder = _RecordingSink()
        with pytest.raises(SandboxArtifactNameCollision, match="differing only by case") as ref:
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(
                        DeclaredOutput(path="run-1/a.png", name="Report.png"),
                        DeclaredOutput(path="run-2/b.png", name="report.png"),
                    ),
                    sink=recorder.sink,
                )
            )
        assert "Report.png" in str(ref.value) and "report.png" in str(ref.value)

    def test_a_consume_output_lands_nothing_however_it_is_named(self):
        sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/run-1/manifest.json": b"{}"})
        recorder = _RecordingSink()
        landed = asyncio.run(
            collect_outputs(
                sandbox,
                _spec(
                    DeclaredOutput(
                        path="run-1/manifest.json",
                        name="manifest.json",
                        disposition=OutputDisposition.CONSUME,
                    )
                ),
                sink=recorder.sink,
            )
        )
        assert landed == ()
        assert recorder.delivered == []


class TestCallTimeOutputs:
    """The road for a kind that knows it lands artifacts and cannot name them in advance."""

    def test_they_are_collected_alongside_what_the_spec_declared(self):
        sandbox = InProcessSandbox(
            seed_files={
                "/maf-sandbox/work/fixed.png": b"a",
                "/maf-sandbox/work/run-1/report.csv": b"bb",
            }
        )
        recorder = _RecordingSink()
        asyncio.run(
            collect_outputs(
                sandbox,
                _spec(DeclaredOutput(path="fixed.png"), at_call_time=True),
                sink=recorder.sink,
                outputs=(DeclaredOutput(path="run-1/report.csv", name="report.csv"),),
            )
        )
        assert recorder.names == ["fixed.png", "report.csv"]

    def test_a_spec_that_does_not_admit_them_refuses_them(self):
        """Such a tool was attached with no sink required and no outbound cap written."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        recorder = _RecordingSink()
        with pytest.raises(ValueError, match="outputs_named_at_call_time"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(),
                    sink=recorder.sink,
                    outputs=(DeclaredOutput(path="a.png"),),
                )
            )
        assert sandbox.calls == []

    def test_the_flag_alone_collects_nothing(self):
        """It is a declaration about the workload, not an instruction to go looking."""
        sandbox = _RecordingSandbox(seed_files={"/maf-sandbox/work/a.png": b"x"})
        recorder = _RecordingSink()
        landed = asyncio.run(collect_outputs(sandbox, _spec(at_call_time=True), sink=recorder.sink))
        assert landed == ()
        assert sandbox.calls == []

    def test_one_cap_counts_both_sources(self):
        """One from each side, so a separate tally per source would still pass the cap and
        deliver twice what the workload allowed."""
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"a", "/maf-sandbox/work/b.png": b"b"}
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=8, max_total_bytes=8, max_files=1)
        with pytest.raises(SandboxTransferCapExceeded, match="max_files=1"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a.png"), files_out=limits, at_call_time=True),
                    sink=recorder.sink,
                    outputs=(DeclaredOutput(path="b.png"),),
                )
            )
        assert recorder.delivered == []

    def test_the_byte_ceiling_counts_both_sources_too(self):
        """`max_files` alone would pass with a generous count and two large files."""
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"aaaa", "/maf-sandbox/work/b.png": b"bbbb"}
        )
        recorder = _RecordingSink()
        limits = TransferLimits(max_bytes_per_file=8, max_total_bytes=6, max_files=8)
        with pytest.raises(SandboxTransferCapExceeded, match="max_total_bytes"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _spec(DeclaredOutput(path="a.png"), files_out=limits, at_call_time=True),
                    sink=recorder.sink,
                    outputs=(DeclaredOutput(path="b.png"),),
                )
            )
        assert recorder.delivered == []


class TestDeliveryFailure:
    def test_a_deliver_that_raises_on_the_first_artifact_lands_nothing(self):
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"a", "/maf-sandbox/work/b.png": b"b"}
        )
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
        sandbox = InProcessSandbox(
            seed_files={"/maf-sandbox/work/a.png": b"a", "/maf-sandbox/work/b.png": b"b"}
        )
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
            SandboxOutputNotConfined,
            SandboxOutputNotRegular,
            SandboxOutputSizeUnknown,
            SandboxOutputUnreachable,
            SandboxTransferCapExceeded,
            SandboxOutputSinkRequired,
            SandboxArtifactNameInvalid,
            SandboxArtifactNameCollision,
            SandboxLandingNotConfined,
        ],
    )
    def test_every_refusal_is_a_sandbox_output_error(self, error: type[Exception]):
        """So a kind that only needs to tell the model "the artifacts did not come back" can
        catch one thing."""
        assert issubclass(error, SandboxOutputError)


def _link_dir(link, target) -> bool:
    """Plant a directory link, however this platform lets one be planted.

    Unprivileged Windows refuses `os.symlink` but allows a junction, and `Path.resolve()`
    follows both — which is the whole of what the sink's check reads. Answers False when
    neither works, so the caller skips rather than passing vacuously.
    """
    import os
    import subprocess
    import sys

    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform != "win32":
        return False
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
    )
    return done.returncode == 0


class TestMakeFileSystemSink:
    """The packaged landing sink: what it writes, and the two escapes it refuses.

    Only one of the two is reachable through `collect_outputs`, so the other is driven
    through `deliver` directly.
    """

    def _artifact(self, name: str, content: bytes = b"payload") -> Artifact:
        return Artifact(name=name, content=content, kind=_KIND, media_type=None)

    def test_it_writes_the_bytes_under_the_root(self, tmp_path):
        sink = make_file_system_sink(tmp_path / "out")
        landed = asyncio.run(sink.deliver(self._artifact("a.txt", b"hello")))

        assert (tmp_path / "out" / "a.txt").read_bytes() == b"hello"
        assert landed.name == "a.txt"
        assert landed.handle == str((tmp_path / "out" / "a.txt").resolve())

    def test_it_creates_the_parents_a_nested_name_needs(self, tmp_path):
        """A landing name may carry separators, and nothing upstream creates the directories."""
        sink = make_file_system_sink(tmp_path / "out")
        asyncio.run(sink.deliver(self._artifact("deep/er/a.txt", b"hi")))

        assert (tmp_path / "out" / "deep" / "er" / "a.txt").read_bytes() == b"hi"

    def test_the_default_display_names_the_artifact_and_its_size(self, tmp_path):
        sink = make_file_system_sink(tmp_path / "out")
        landed = asyncio.run(sink.deliver(self._artifact("a.txt", b"1234")))

        assert landed.display == "a.txt (4 bytes)"

    def test_a_kind_can_supply_its_own_display(self, tmp_path):
        """Sample 07 introduces its artifacts with a verb and sample 08 deliberately without
        one, so the line the model sees cannot be the sink's to fix."""
        sink = make_file_system_sink(
            tmp_path / "out",
            display=lambda artifact, path: f"Rendered {artifact.name} in {path.parent.name}/",
        )
        landed = asyncio.run(sink.deliver(self._artifact("a.png")))

        assert landed.display == "Rendered a.png in out/"

    def test_a_name_resolving_outside_the_root_is_refused(self, tmp_path):
        """`validate_artifact_name` refuses `..` upstream, so this is defence the sink owes
        anyway: it is a public helper and nothing guarantees every caller ran that first."""
        root = tmp_path / "out"
        sink = make_file_system_sink(root)

        with pytest.raises(SandboxLandingNotConfined, match="outside"):
            asyncio.run(sink.deliver(self._artifact("../escaped.txt")))

        assert not (tmp_path / "escaped.txt").exists()

    def test_the_refusal_names_no_host_path(self, tmp_path):
        """`maf-sandbox-codeact` interpolates this family straight into the tool result the
        model reads, so a host path in the message reaches the transcript — the leak
        `LandedArtifact.handle` exists to prevent. Every other member of the family says only
        the guest-supplied name, and this one has to as well."""
        root = tmp_path / "out"
        sink = make_file_system_sink(root)

        with pytest.raises(SandboxLandingNotConfined) as caught:
            asyncio.run(sink.deliver(self._artifact("../escaped.txt")))

        message = str(caught.value)
        assert "escaped.txt" in message, "the guest's own name is what a caller can act on"
        for leaked in (str(root), str(root.resolve()), str(tmp_path)):
            assert leaked not in message, f"the refusal named a host path: {leaked}"

    def test_a_link_already_in_the_root_is_refused_rather_than_followed(self, tmp_path):
        """The case the lexical name check cannot see, and the one that made this a helper.

        A name that is perfectly valid lands somewhere else entirely because a component of
        the destination is a link planted before the run.
        """
        root = tmp_path / "out"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        if not _link_dir(root / "sub", outside):
            pytest.skip("this platform will not let the test plant a directory link")

        sink = make_file_system_sink(root)
        with pytest.raises(SandboxLandingNotConfined):
            asyncio.run(sink.deliver(self._artifact("sub/a.txt")))

        assert not (outside / "a.txt").exists(), "it wrote through the link before refusing"

    def test_it_lands_a_whole_collection_through_collect_outputs(self, tmp_path):
        """End to end, because `deliver` alone does not prove the sink is shaped like one."""
        sandbox = InProcessSandbox()
        asyncio.run(
            sandbox.write_file(f"{_WORK_DIR}/report.md", b"# hi", working_directory=_WORK_DIR)
        )
        spec = _spec(DeclaredOutput(path="report.md", media_type="text/markdown"))

        landed = asyncio.run(
            collect_outputs(sandbox, spec, sink=make_file_system_sink(tmp_path / "out"))
        )

        assert [a.name for a in landed] == ["report.md"]
        assert (tmp_path / "out" / "report.md").read_bytes() == b"# hi"


class TestTheNameThisHadBefore:
    """`portable_name` warns on lookup and hands back `portable_file_name`."""

    def test_it_warns_and_delegates(self):
        import maf_sandbox

        with pytest.warns(DeprecationWarning, match="portable_file_name"):
            rewritten = maf_sandbox.portable_name("NUL.txt")

        assert rewritten == maf_sandbox.portable_file_name("NUL.txt")

    def test_both_spellings_stay_importable_for_the_cycle(self):
        import maf_sandbox

        assert "portable_name" in maf_sandbox.__all__
        assert "portable_file_name" in maf_sandbox.__all__
