"""Every sample's `_scaffold.py` is one file, copied. This is what holds the copies together.

The duplication is deliberate and the reasoning is in `_scaffold.py`'s own docstring: a sample
must run from a downloaded directory against wheels from PyPI, so it cannot import a module
that only exists in this repository, and publishing one would make it API. That leaves
copying, and copying without a test is how eight files drift into eight behaviours.

Same precedent as `maf-sandbox-docker`'s proxy: duplicate, then pin.

This suite is at the repository root rather than inside a package because `samples/` belongs to
no package — it is not a uv workspace member and not in any package's `testpaths`, so nothing
else would ever look at it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SCAFFOLD = "_scaffold.py"

#: The sample directories, discovered rather than listed. A ninth sample that forgets its
#: scaffold has to fail here, and a hardcoded list is exactly how it would not.
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def test_the_sample_directories_were_found():
    """A glob that matches nothing would make every assertion below vacuously true."""
    assert len(_SAMPLE_DIRS) >= 8, f"found {len(_SAMPLE_DIRS)} sample directories under {_SAMPLES}"


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_every_sample_carries_the_scaffold(sample: Path):
    assert (sample / _SCAFFOLD).is_file(), (
        f"{sample.name} has no {_SCAFFOLD}. Every sample needs its own copy — it cannot import "
        "one from outside its directory and still run from a download."
    )


def test_every_copy_is_byte_identical():
    """Byte-identical, not merely equivalent.

    A difference that looks cosmetic is still a difference a reader has to explain, and the
    only honest answer for a file that exists eight times is that there is nothing to explain.
    """
    copies = {sample.name: (sample / _SCAFFOLD).read_bytes() for sample in _SAMPLE_DIRS}
    canonical_name, canonical = next(iter(copies.items()))
    drifted = sorted(name for name, body in copies.items() if body != canonical)
    assert not drifted, (
        f"{_SCAFFOLD} has drifted in: {', '.join(drifted)}. It differs from {canonical_name}'s "
        "copy. Change one, change all of them — or move what differs into the sample's own "
        "`agent.py`, which is where anything sample-specific belongs."
    )


def test_no_sample_still_defines_the_helper_itself():
    """The point of the scaffold is that `agent.py` stops carrying this.

    It was written out eight times before ([#209](https://github.com/sokolaidev/maf-extensions/issues/209)),
    so a sample that reintroduces it is a regression rather than a style choice.
    """
    offenders = [
        sample.name
        for sample in _SAMPLE_DIRS
        if "def require_env_vars" in (sample / "agent.py").read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"these samples define `require_env_vars` in `agent.py`: {', '.join(offenders)}. "
        f"Import it from {_SCAFFOLD} instead."
    )
