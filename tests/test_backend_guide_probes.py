"""Every probe the backend-writing guide names is a probe that exists.

`docs/sandbox/backends/writing-a-backend.md` is the ordered path through what a backend owes,
and its **Proved by** lines are what keep it honest: each name is a probe in
`maf_sandbox.conformance`, and a name that stopped being one is the guide claiming a proof it
no longer has. The helpers it tells an author to reach for are held the same way — a
backticked name in a `maf_sandbox.paths` or `maf_sandbox.conformance` shape must be a name
that module still exports.

Two traps, both worth knowing before trusting this.

**It is a wiring check.** It proves every name the guide writes resolves, not that the guide
maps the right probes to the right method — that mapping is editorial, and review rather than
this file is where it is caught.

**It pins names, not prose.** An Owes line can outlive its docstring and nothing here fires;
the doc-structure checks own the links and the Status rows, and this owns the identifiers.
"""

from __future__ import annotations

import re
from pathlib import Path

from maf_sandbox import conformance, paths

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDE = REPO_ROOT / "docs" / "sandbox" / "backends" / "writing-a-backend.md"

#: The five registries a **Proved by** line draws from.
REGISTRIES = (
    conformance.FILES_OUT_PROBES,
    conformance.FILES_IN_PROBES,
    conformance.EXEC_PROBES,
    conformance.FILES_DELETE_PROBES,
    conformance.RECLAIM_PROBES,
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

_CODE_SPAN = re.compile(r"`([^`]+)`")
_PROVED_BY_LINE = re.compile(r"^\s*[-*]\s*\*\*Proved by:\*\*\s*(.*)$", re.MULTILINE)
_METHOD_HEADING = re.compile(r"^### ", re.MULTILINE)
FOUR_LINES = ("**Owes:**", "**Use:**", "**Never:**", "**Proved by:**")


def test_every_probe_the_guide_names_exists() -> None:
    proved = [
        match.group(1) for match in _PROVED_BY_LINE.finditer(GUIDE.read_text(encoding="utf-8"))
    ]
    assert proved, "the guide writes no **Proved by** line at all"
    for line in proved:
        for span in _CODE_SPAN.findall(line):
            assert span in PROBE_NAMES, f"the guide's Proved by names {span!r}, which is no probe"


def test_every_method_carries_the_four_lines() -> None:
    markdown = GUIDE.read_text(encoding="utf-8")
    methods = len(_METHOD_HEADING.findall(markdown))
    assert methods, "the guide has no method entries"
    for marker in FOUR_LINES:
        found = markdown.count(marker)
        assert found == methods, f"{marker} appears {found} times for {methods} methods"


def test_every_helper_the_guide_names_is_exported() -> None:
    for span in frozenset(_CODE_SPAN.findall(GUIDE.read_text(encoding="utf-8"))):
        if span.startswith(PATHS_SHAPES):
            assert span in paths.__all__, (
                f"the guide names {span!r}, which maf_sandbox.paths no longer exports"
            )
        if span.startswith(CONFORMANCE_SHAPES) or span.endswith("_PROBES") or RUNNER.match(span):
            assert span in conformance.__all__, (
                f"the guide names {span!r}, which maf_sandbox.conformance no longer exports"
            )
