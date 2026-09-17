"""The prepared image build: only preparation reaches the network, and one stage feeds the rest."""

from __future__ import annotations

import itertools
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


def stages() -> dict[str, list[list[str]]]:
    """Each stage's RUN, COPY and ADD instructions as token lists, keyed by stage name.

    Continuation lines are joined into one instruction; heredoc bodies are skipped.
    """
    found: dict[str, list[list[str]]] = {}
    current, marker, pending = "", None, ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if marker is not None:
            marker = None if line == marker else marker
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        tokens = (pending + line).split()
        pending = ""
        # Dockerfile keywords are case-insensitive, so `run` and `as` count too.
        tokens[:1] = [token.upper() for token in tokens[:1]]
        if tokens[:1] == ["FROM"]:
            tokens[2:3] = [token.upper() for token in tokens[2:3]]
            assert len(tokens) in (2, 4) and tokens[2:3] in ([], ["AS"]), tokens
            current = tokens[3] if len(tokens) == 4 else "image"
            found[current] = []
        elif tokens[:1] in (["RUN"], ["COPY"], ["ADD"]):
            found[current].append(tokens)
            if heredoc := re.fullmatch(r"<<'(\w+)'", tokens[-1]):
                marker = heredoc.group(1)
    return found


def flags(instruction: list[str]) -> list[str]:
    """The options between an instruction's keyword and its first argument."""
    return list(itertools.takewhile(lambda token: token.startswith("--"), instruction[1:]))


def test_only_the_preparation_stage_reaches_the_network():
    found = stages()
    assert list(found) == ["prepare", "prepared", "unpack", "image"]
    assert [" ".join(tokens) for tokens in found["prepare"] if tokens[0] == "RUN"] == [PREPARE]
    later = [tokens for name in ("unpack", "image") for tokens in found[name] if tokens[0] == "RUN"]
    # Any second flag, such as --mount, could bind the build context into a network-less step.
    assert later and all(flags(tokens) == ["--network=none"] for tokens in later)


def test_later_stages_take_the_preparation_only_from_the_prepared_stage():
    found = stages()
    assert not [tokens for stage in found.values() for tokens in stage if tokens[0] == "ADD"]
    assert found["prepared"] == [["COPY", "--from=prepare", "/prepared/", "/"]]
    copies = [
        tokens for name in ("unpack", "image") for tokens in found[name] if tokens[0] == "COPY"
    ]
    assert copies and all(
        flags(tokens) in (["--from=prepared"], ["--from=unpack"]) for tokens in copies
    )


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
