"""What every package's README owes the PyPI project page.

A packaged README is not documentation that happens to sit beside the code — `readme =` in
`pyproject.toml` makes it the wheel's `Description`, which is the whole of what PyPI renders.
So it is served **standalone**, detached from the repository it was written in, and a link
that only resolves inside that repository is a failure no repo-relative check can see.

`scripts/check_doc_paths.py` is the reason to say that plainly. It resolves a README's links
against the tree and passes them, correctly, because the files are there. `maf-sandbox-otel`
0.1.0 shipped `[LICENSE](LICENSE)` that way: valid in the repository, and a 404 on the project
page, where the same href means `https://pypi.org/project/maf-sandbox-otel/LICENSE`.

The conventions here were followed by six packages and enforced by nothing, so the seventh
missed all three at once — no badge row, no experimental banner, and that dead link. Six
agreeing is not a rule; this is.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

#: The same shape `tests/test_docs_structure.py` reads links with, so the two agree on what a
#: link is: an optional `<…>` around the target, and any title text after it.
_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?[^)]*\)")

#: A target that survives being served from another host. `#` is an in-page anchor, which PyPI
#: renders as one; everything else has to name its own origin.
_RESOLVES_ANYWHERE = ("http://", "https://", "mailto:", "#")

_READMES = sorted(_ROOT.glob("packages/*/README.md"))


def _ids(paths: list[Path]) -> list[str]:
    return [path.parent.name for path in paths]


def _distribution(readme: Path) -> str:
    """The name PyPI serves this README under, read from the package's own metadata."""
    pyproject = tomllib.loads((readme.parent / "pyproject.toml").read_text("utf-8"))
    return pyproject["project"]["name"]


def _warning_class(distribution: str) -> str:
    """`maf-sandbox-otel` -> `MafSandboxOtelExperimentalWarning`, the class it warns with."""
    return "".join(part.title() for part in distribution.split("-")) + "ExperimentalWarning"


def test_there_are_packages_to_check():
    """A glob that matches nothing passes every parametrised test below without running one."""
    assert len(_READMES) >= 7, f"only {len(_READMES)} packaged README(s) found under packages/"


@pytest.mark.parametrize("readme", _READMES, ids=_ids(_READMES))
class TestEveryPackagedReadmeStandsAlone:
    def test_no_link_needs_the_repository_to_resolve(self, readme: Path):
        """A relative href is a dead link on the project page, and only there.

        Named rather than counted: the failure is one line in a file of prose, and a bare count
        sends the reader looking for it.
        """
        relative = [
            target
            for target in _LINK.findall(readme.read_text("utf-8"))
            if not target.startswith(_RESOLVES_ANYWHERE)
        ]
        assert not relative, (
            f"{readme.parent.name}/README.md ships to PyPI, where these resolve against "
            f"pypi.org rather than this repository: {', '.join(sorted(set(relative)))}. "
            "Write them as full https:// URLs — check_doc_paths.py cannot catch this, because "
            "in the tree they are correct."
        )

    def test_the_badge_row_names_this_distribution(self, readme: Path):
        """The row is per-package: copied from a sibling it advertises the sibling's version."""
        distribution = _distribution(readme)
        text = readme.read_text("utf-8")
        for badge in (
            f"https://img.shields.io/pypi/v/{distribution})",
            f"https://img.shields.io/pypi/pyversions/{distribution})",
            "https://img.shields.io/badge/license-MIT-green)",
        ):
            assert badge in text, f"{readme.parent.name}/README.md carries no {badge} badge"

    def test_the_experimental_banner_names_this_package(self, readme: Path):
        """Pre-1.0 is the one thing a reader must not have to go looking for.

        The banner names the warning class the package actually raises on import, so it is a
        claim about behaviour rather than a disclaimer, and a copied one names the wrong class.
        """
        text = readme.read_text("utf-8")
        assert "> **Experimental.**" in text, (
            f"{readme.parent.name}/README.md carries no experimental banner, and the package "
            "warns on import that it is pre-1.0"
        )
        expected = _warning_class(_distribution(readme))
        assert expected in text, f"{readme.parent.name}/README.md's banner does not name {expected}"
