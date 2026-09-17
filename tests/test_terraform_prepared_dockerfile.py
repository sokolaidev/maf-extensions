"""The prepared image build: only preparation reaches the network, and one stage feeds the rest."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "images/terraform-sandbox/prepared.Dockerfile"
sys.path.insert(0, str(ROOT / "scripts"))
import terraform_dependencies as prep  # noqa: E402

PREPARE = (
    "RUN python3 -I /src/scripts/terraform_dependencies.py"
    " --manifest /src/manifest.json --output /prepared"
)


def stages() -> dict[str, list[str]]:
    """Each stage's RUN, COPY and ADD instructions, keyed by stage name; heredocs are skipped."""
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
        elif line.startswith(("RUN ", "COPY ", "ADD ")):
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
    # A second flag such as --mount could bind the build context into a network-less step.
    assert later and all(re.match(r"RUN --network=none (?!--)", line) for line in later)


def test_later_stages_take_the_preparation_only_from_the_prepared_stage():
    found = stages()
    assert not [line for lines in found.values() for line in lines if line.startswith("ADD ")]
    assert found["prepared"] == ["COPY --from=prepare /prepared/ /"]
    copies = [
        line for name in ("unpack", "image") for line in found[name] if line.startswith("COPY ")
    ]
    assert copies and all(re.match(r"COPY --from=(prepared|unpack) ", line) for line in copies)


def heredoc_step(marker: str) -> str:
    """The Python body of the one heredoc RUN step that mentions ``marker``."""
    bodies, body = [], None
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if body is not None:
            if line == "PY":
                bodies.append("\n".join(body))
                body = None
            else:
                body.append(line)
        elif line.startswith("RUN ") and line.endswith("<<'PY'"):
            body = []
    (found,) = [text for text in bodies if marker in text]
    return found


def run_install_check(install: Path) -> None:
    """Run the image's verification step against a temporary install root."""
    step = heredoc_step("reader_sha256")
    assert step.count('Path("/opt/maf-terraform")') == 1
    step = step.replace('Path("/opt/maf-terraform")', f"Path({install.as_posix()!r})")
    exec(compile(step, str(DOCKERFILE), "exec"), {"__name__": "__main__"})


@pytest.fixture
def install(tmp_path: Path) -> Path:
    shutil.copyfile(ROOT / "images/terraform-sandbox/runner.py", tmp_path / "runner.py")
    (tmp_path / "registry").mkdir()
    (tmp_path / "engine.json").write_text(json.dumps({"engine": "terraform"}))
    receipt = {
        "engine": "terraform",
        "manifest_sha256": "m",
        "policy_sha256": "p",
        "policy_contract": prep.policy_contract(),
        "registry_modules": [],
    }
    (tmp_path / "dependencies.json").write_text(json.dumps(receipt))
    return tmp_path


def test_a_base_with_the_preparing_launcher_is_accepted(install: Path):
    run_install_check(install)
    assert json.loads((install / "engine.json").read_text())["profile"] == "prepared"


def test_a_base_whose_launcher_differs_is_refused(install: Path):
    with (install / "runner.py").open("a", encoding="utf-8") as launcher:
        launcher.write("# changed\n")
    with pytest.raises(AssertionError, match="rebuild the base from this checkout"):
        run_install_check(install)


def test_a_receipt_without_the_reader_digest_is_refused(install: Path):
    receipt = json.loads((install / "dependencies.json").read_text())
    del receipt["policy_contract"]["reader_sha256"]
    (install / "dependencies.json").write_text(json.dumps(receipt))
    with pytest.raises(AssertionError, match="rebuild the base from this checkout"):
        run_install_check(install)
