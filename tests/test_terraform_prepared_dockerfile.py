"""The prepared image build: only preparation reaches the network, and one stage feeds the rest."""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "images/terraform-sandbox/prepared.Dockerfile"
PREPARE = (
    "RUN python3 -I /src/scripts/terraform_dependencies.py"
    " --manifest /src/manifest.json --output /prepared"
)


def stages() -> dict[str, list[str]]:
    """Each stage's RUN and COPY instructions, keyed by stage name; heredoc bodies are skipped."""
    found: dict[str, list[str]] = {}
    current, marker = "", None
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if marker is not None:
            marker = None if line == marker else marker
            continue
        if line.startswith("FROM "):
            match = re.fullmatch(r"FROM \S+(?: AS (\S+))?", line)
            assert match, line
            current = match.group(1) or "image"
            found[current] = []
        elif line.startswith(("RUN ", "COPY ")):
            found[current].append(line)
            if heredoc := re.search(r"<<'(\w+)'$", line):
                marker = heredoc.group(1)
    return found


def test_only_the_preparation_stage_reaches_the_network():
    found = stages()
    assert list(found) == ["prepare", "prepared", "unpack", "image"]
    assert [line for line in found["prepare"] if line.startswith("RUN ")] == [PREPARE]
    later = [
        line for name in ("unpack", "image") for line in found[name] if line.startswith("RUN ")
    ]
    assert later and all(line.startswith("RUN --network=none ") for line in later)


def test_later_stages_take_the_preparation_only_from_the_prepared_stage():
    found = stages()
    assert found["prepared"] == ["COPY --from=prepare /prepared/ /"]
    copies = [
        line for name in ("unpack", "image") for line in found[name] if line.startswith("COPY ")
    ]
    assert copies and all(re.match(r"COPY --from=(prepared|unpack) ", line) for line in copies)
