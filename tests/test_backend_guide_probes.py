"""Every probe the backend-writing guide names is a probe that exists.

`docs/sandbox/backends/writing-a-backend.md` is the ordered path through what a backend owes,
and its **Proved by** lines are what keep it honest: each name is a probe in
`maf_sandbox.conformance`, and a name that stopped being one is the guide claiming a proof it
no longer has. The helpers it tells an author to reach for are held the same way — a
backticked name in a `maf_sandbox.paths` or `maf_sandbox.conformance` shape must be a name
that module still exports.

Three traps, all worth knowing before trusting this.

**It is a wiring check.** It proves every name the guide writes resolves, not that the guide
maps the right probes to the right method — that mapping is editorial, and review rather than
this file is where it is caught.

**It pins names, not prose.** An Owes line can outlive its docstring and nothing here fires;
the doc-structure checks own the links and the Status rows, and this owns the identifiers.

**Method coverage is pinned to the protocol.** The entries are complete when they cover exactly
`Sandbox`'s public surface — a member added there fails here until the guide gains its entry —
and each entry carries its own four lines, because a count that balances one method's omission
against another's duplication proves nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import maf_sandbox.conformance
import maf_sandbox.paths

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDE = REPO_ROOT / "docs" / "sandbox" / "backends" / "writing-a-backend.md"

#: The five registries a **Proved by** line draws from.
REGISTRIES = (
    maf_sandbox.conformance.FILES_OUT_PROBES,
    maf_sandbox.conformance.FILES_IN_PROBES,
    maf_sandbox.conformance.EXEC_PROBES,
    maf_sandbox.conformance.FILES_DELETE_PROBES,
    maf_sandbox.conformance.RECLAIM_PROBES,
)
PROBE_NAMES = frozenset(probe.name for registry in REGISTRIES for probe in registry)

#: The shapes of a `maf_sandbox.paths` export. Any backticked span the guide writes in one of
#: these shapes must be a name the module still exports, so a renamed or removed helper fails
#: here before it misleads an author following the guide.
PATHS_SHAPES = (
    "confine_",
    "refuse_",
    "guest_path_",
    "path_ancestors_",
    "stat_by_asking_",
    "tar_header_",
    "sandbox_entry_",
)

#: The shapes of a `maf_sandbox.conformance` export: `assert_` and `measure_` prefix its entry
#: points, `*_PROBES` its registries. `run_code` is a protocol member that shares the runner
#: prefix, so the runner shape is anchored at both ends rather than matched by prefix.
CONFORMANCE_SHAPES = ("assert_", "measure_")
RUNNER = re.compile(r"^run_\w+_probes$")

#: The guide's method entries are complete when they cover exactly the `Sandbox` protocol's
#: public surface, so a member added there fails here until the guide gains its entry.
PROTOCOL_METHODS = frozenset(name for name in dir(maf_sandbox.Sandbox) if not name.startswith("_"))

#: The exported vocabulary across the modules the guide cites, so a backticked class or
#: constant is still exported somewhere it plausibly lives.
EXPORTS = (
    frozenset(maf_sandbox.__all__)
    | frozenset(maf_sandbox.conformance.__all__)
    | frozenset(maf_sandbox.paths.__all__)
)

#: The shapes of that vocabulary as the guide spells it, matched on the first dotted segment —
#: `EntryKind.SYMLINK` is a member of `EntryKind`, and the member is md-blocks' business.
VOCAB_SHAPES = (
    "Sandbox",
    "Backend",
    "Conformance",
    "Posix",
    "Probe",
    "Entry",
    "Egress",
    "Capability",
    "Isolation",
    "DEFAULT_",
)

_CODE_SPAN = re.compile(r"`([^`]+)`")
_PROVED_BY_LINE = re.compile(r"^\s*[-*]\s*\*\*Proved by:\*\*\s*(.*)$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6}) (.*)$", re.MULTILINE)
#: Probe names are kebab-cased, and the existence check reads only backticked spans — so a
#: name written without backticks would pass unheard of. Refusing the shape on a Proved-by
#: line is what closes that.
_KEBAB = re.compile(r"\b[a-z]+(?:-[a-z0-9]+)+\b")
FOUR_LINES = ("**Owes:**", "**Use:**", "**Never:**", "**Proved by:**")


def _method_sections(markdown: str) -> dict[str, str]:
    """Each method entry's name and body, bounded by the next heading of any level."""
    headings = [
        (match.start(), len(match.group(1)), match.group(2))
        for match in _HEADING.finditer(markdown)
    ]
    sections: dict[str, str] = {}
    for index, (start, level, title) in enumerate(headings):
        if level != 3:
            continue
        name = _CODE_SPAN.fullmatch(title.strip())
        assert name, f"method heading {title!r} does not name its method in backticks"
        key = name.group(1)
        assert key not in sections, (
            f"method heading {key!r} appears twice; the second would overwrite the first"
        )
        end = headings[index + 1][0] if index + 1 < len(headings) else len(markdown)
        sections[key] = markdown[start:end]
    return sections


def test_every_probe_the_guide_names_exists() -> None:
    proved = [
        match.group(1) for match in _PROVED_BY_LINE.finditer(GUIDE.read_text(encoding="utf-8"))
    ]
    assert proved, "the guide writes no **Proved by** line at all"
    for line in proved:
        for span in _CODE_SPAN.findall(line):
            assert span in PROBE_NAMES, f"the guide's Proved by names {span!r}, which is no probe"
        assert not _KEBAB.findall(_CODE_SPAN.sub("", line)), (
            f"a probe-shaped name outside backticks on a Proved-by line: {line!r}"
        )


def test_the_guide_covers_the_protocol_and_each_entry_carries_the_four_lines() -> None:
    sections = _method_sections(GUIDE.read_text(encoding="utf-8"))
    assert sections, "the guide has no method entries"
    assert frozenset(sections) == PROTOCOL_METHODS, (
        f"method entries {sorted(sections)} against the protocol's {sorted(PROTOCOL_METHODS)}"
    )
    for name, body in sections.items():
        for marker in FOUR_LINES:
            assert marker in body, f"the {name} entry lacks {marker}"


def test_every_helper_the_guide_names_is_exported() -> None:
    for span in frozenset(_CODE_SPAN.findall(GUIDE.read_text(encoding="utf-8"))):
        if span.startswith(PATHS_SHAPES):
            assert span in maf_sandbox.paths.__all__, (
                f"the guide names {span!r}, which maf_sandbox.paths no longer exports"
            )
        if span.startswith(CONFORMANCE_SHAPES) or span.endswith("_PROBES") or RUNNER.match(span):
            assert span in maf_sandbox.conformance.__all__, (
                f"the guide names {span!r}, which maf_sandbox.conformance no longer exports"
            )
        first = span.split(".")[0]
        if first.startswith(VOCAB_SHAPES):
            assert first in EXPORTS, f"the guide names {span!r}, which no cited module exports"
