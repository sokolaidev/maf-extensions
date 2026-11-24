"""Select bounded diagnostic presence facts without promoting engine prose."""

import json
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A checked severity and an optional, untrusted engine-relative filename."""

    severity: Literal["error", "warning"]
    filename: str | None


def diagnostic_summary(diagnostics: Sequence[Diagnostic], staged: dict[str, str], root: str) -> str:
    """Report presence per staged input and severity, with at most 128 distinct pairs."""
    if len(staged) > 64:
        raise ValueError("manifest exceeds the diagnostic reference vocabulary")
    # The CLI runs in the root module; sibling modules can legitimately begin with ../.
    references = {posixpath.relpath(path, root): reference for path, reference in staged.items()}
    found: set[tuple[str, str]] = set()
    unattributed = False
    for diagnostic in diagnostics:
        reference = references.get(diagnostic.filename) if diagnostic.filename is not None else None
        if reference is None:
            unattributed = True
        else:
            found.add((reference, diagnostic.severity))
    return json.dumps(
        {
            "type": "terraform_diagnostics",
            "diagnostics": [
                {"file": reference, "severity": severity}
                for reference in staged.values()
                for severity in ("error", "warning")
                if (reference, severity) in found
            ],
            "unattributed_diagnostics": unattributed,
        },
        separators=(",", ":"),
    )
