"""The publish-time guard against shipping a core the dependents can no longer import.

`scripts/check_published_dependents_work.py` is the other half of the admit check: the admit
check reads each dependent's ceiling and refuses if it excludes the version going out; this one
installs the candidate core wheel beside each dependent *version* that admits it and refuses if
that version no longer imports. Its at-risk filter and its verdict are pure functions of
metadata, so both are tested here; the PyPI fetches and the venv install/import are the impure
steps — the fetches are pinned by mocking ``urllib.request.urlopen`` and the install/import by
mocking ``subprocess.run``, so no venv is created, uv is never invoked, and no network is hit.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for shared comparisons
_spec = importlib.util.spec_from_file_location(
    "check_published_dependents_work", _SCRIPTS / "check_published_dependents_work.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# The retry the fetches inherit; `_patch_urlopen` zeroes its pause so no test waits.
import pypi_index  # noqa: E402

_ARGV0 = "scripts/check_published_dependents_work.py"


class TestImportModule:
    @pytest.mark.parametrize(
        ("distribution", "module"),
        [
            ("maf-sandbox", "maf_sandbox"),
            ("maf-sandbox-bicep", "maf_sandbox_bicep"),
            ("maf-sandbox-docker", "maf_sandbox_docker"),
        ],
    )
    def test_the_dash_becomes_an_underscore(self, distribution: str, module: str):
        assert check.import_module(distribution) == module


class TestAtRisk:
    """The inverse of the admit check, per published version: ceiling is None or admits."""

    def test_a_ceiling_that_admits_is_at_risk(self):
        # bicep <0.13 admitted 0.11.0 — the dependent version the admit check passes and this tests.
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox<0.13,>=0.10.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_a_ceiling_that_excludes_is_not_at_risk(self):
        # <0.3 correctly refused 0.11.0; that is the admit check's refusal, not this check's job.
        published = {"maf-sandbox-bicep": [("0.3.0", ["maf-sandbox<0.3,>=0.2.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == []

    def test_an_unbounded_ceiling_is_at_risk(self):
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox>=0.10.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.at_risk({"maf-sandbox-new": None}, (0, 11, 0)) == []

    def test_the_order_pypi_returns_is_irrelevant(self):
        # The admit check parses order-independently; this check inherits that, so it must too.
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox>=0.10.0,<0.13"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_an_old_loose_ceiling_version_is_at_risk_even_when_latest_moved_on(self):
        # Both an old and a current admitting version are selected — the old one is the case
        # latest-only misses, so it must appear alongside the current one.
        published = {
            "maf-sandbox-docker": [
                ("0.2.0", ["maf-sandbox<0.12,>=0.10.0"]),  # admits 0.11.0
                ("0.6.0", ["maf-sandbox<0.13,>=0.11.0"]),  # admits 0.11.0 too
            ]
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-docker", "0.2.0"),
            ("maf-sandbox-docker", "0.6.0"),
        ]

    def test_versions_are_sorted_within_a_dependent(self):
        published = {
            "maf-sandbox-bicep": [
                ("0.10.0", ["maf-sandbox<0.13"]),
                ("0.2.0", ["maf-sandbox<0.13"]),
                ("0.9.0", ["maf-sandbox<0.13"]),
            ]
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-bicep", "0.2.0"),
            ("maf-sandbox-bicep", "0.9.0"),
            ("maf-sandbox-bicep", "0.10.0"),
        ]

    def test_dependents_are_sorted_by_name(self):
        published = {
            "maf-sandbox-wslc": [("0.5.0", ["maf-sandbox<0.13"])],
            "maf-sandbox-acas": [("0.5.0", ["maf-sandbox<0.13"])],
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-acas", "0.5.0"),
            ("maf-sandbox-wslc", "0.5.0"),
        ]

    def test_it_is_the_inverse_of_the_admit_check_refusals(self):
        published = {
            "maf-sandbox-acas": [("0.5.0", ["maf-sandbox<0.7,>=0.6.0"])],  # excludes 0.11.0
            "maf-sandbox-bicep": [("0.5.6", ["maf-sandbox<0.13,>=0.10.0"])],  # admits 0.11.0
            "maf-sandbox-docker": [("0.6.0", ["maf-sandbox>=0.6.0"])],  # unbounded
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-bicep", "0.5.6"),
            ("maf-sandbox-docker", "0.6.0"),
        ]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://pypi.org/", code, "err", email.message.Message(), io.BytesIO(b"")
    )


class _Response:
    """The slice of an HTTP response ``json.load`` reads: a ``read()`` returning JSON bytes."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, object],
) -> None:
    """Route a URL to a payload dict (success) or an error to raise.

    Keys are matched as substrings, longest first, so a per-version segment like
    ``bicep/0.2.0/json`` is not shadowed by the top-level ``bicep/json``. A list value is
    consumed one reply per call and its last entry repeats, which is how a retried failure is
    written.
    """

    def fake_urlopen(url: str | urllib.request.Request, timeout: int | None = None) -> _Response:
        target = url.full_url if isinstance(url, urllib.request.Request) else url
        for key in sorted(routes, key=len, reverse=True):
            if key in target:
                result = routes[key]
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                if isinstance(result, BaseException):
                    raise result
                return _Response(result)
        raise AssertionError(f"unexpected url {target}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # The retry is pinned in tests/test_pypi_index.py; here it only has to cost no seconds.
    monkeypatch.setattr(pypi_index, "FIRST_PAUSE_SECONDS", 0.0)


class TestFetchRequiresDistForVersion:
    """``fetch_requires_dist_for_version`` reads one per-version JSON, same yanked/404 rules."""

    def test_returns_that_versions_requirements(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {
                        "requires_dist": ["maf-sandbox<0.12,>=0.10.0"],
                        "yanked": False,
                    }
                }
            },
        )
        assert check.fetch_requires_dist_for_version("maf-sandbox-bicep", "0.2.0") == [
            "maf-sandbox<0.12,>=0.10.0"
        ]

    def test_a_yanked_version_is_skipped(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.12"], "yanked": True}
                }
            },
        )
        assert check.fetch_requires_dist_for_version("maf-sandbox-bicep", "0.2.0") is None

    def test_a_missing_version_is_skipped(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"maf-sandbox-bicep/0.2.0/json": _http_error(404)})
        assert check.fetch_requires_dist_for_version("maf-sandbox-bicep", "0.2.0") is None

    def test_a_5xx_that_outlasts_the_retries_is_fatal(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"maf-sandbox-bicep/0.2.0/json": _http_error(500)})
        with pytest.raises(pypi_index.IndexUnreachable):
            check.fetch_requires_dist_for_version("maf-sandbox-bicep", "0.2.0")

    def test_one_reset_costs_a_retry_rather_than_the_run(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "maf-sandbox-bicep/0.2.0/json": [
                    urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
                    {"info": {"requires_dist": ["maf-sandbox<0.12"], "yanked": False}},
                ]
            },
        )
        assert check.fetch_requires_dist_for_version("maf-sandbox-bicep", "0.2.0") == [
            "maf-sandbox<0.12"
        ]


class TestFetchVersionRequirements:
    """``fetch_version_requirements`` resolves the version list from the simple index."""

    def test_every_version_listed_by_the_simple_index_is_fetched_per_version(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.0.1", "0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13,>=0.11.0"], "yanked": False}
                },
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.12,>=0.10.0"], "yanked": False}
                },
                "maf-sandbox-bicep/0.0.1/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.9,>=0.8.0"], "yanked": False}
                },
            },
        )
        assert check.fetch_version_requirements("maf-sandbox-bicep") == {
            "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
            "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
            "0.0.1": ["maf-sandbox<0.9,>=0.8.0"],
        }

    def test_a_stale_top_level_document_no_longer_matters(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "maf-sandbox-bicep/json": {
                    "info": {"version": "0.5.0", "requires_dist": ["maf-sandbox<0.13"]},
                    "releases": {"0.5.0": [{}], "0.2.0": [{}]},
                },
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.5.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13,>=0.11.0"], "yanked": False}
                },
                "maf-sandbox-bicep/0.5.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": False}
                },
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.12,>=0.10.0"], "yanked": False}
                },
            },
        )
        by_version = check.fetch_version_requirements("maf-sandbox-bicep")
        assert by_version is not None
        assert "0.6.0" in by_version

    def test_a_yanked_version_is_skipped(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": True}
                },
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.12"], "yanked": False}
                },
            },
        )
        assert check.fetch_version_requirements("maf-sandbox-bicep") == {
            "0.2.0": ["maf-sandbox<0.12"]
        }

    def test_a_per_version_404_is_skipped_not_fatal(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": False}
                },
                "maf-sandbox-bicep/0.2.0/json": _http_error(404),
            },
        )
        assert check.fetch_version_requirements("maf-sandbox-bicep") == {
            "0.6.0": ["maf-sandbox<0.13"]
        }

    def test_a_never_released_distribution_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"simple/maf-sandbox-bicep/": _http_error(404)})
        assert check.fetch_version_requirements("maf-sandbox-bicep") is None

    def test_a_5xx_on_the_simple_index_that_outlasts_the_retries_is_fatal(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"simple/maf-sandbox-bicep/": _http_error(500)})
        with pytest.raises(pypi_index.IndexUnreachable):
            check.fetch_version_requirements("maf-sandbox-bicep")

    def test_a_5xx_on_a_per_version_fetch_that_outlasts_the_retries_is_fatal(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": False}
                },
                "maf-sandbox-bicep/0.2.0/json": _http_error(500),
            },
        )
        with pytest.raises(pypi_index.IndexUnreachable):
            check.fetch_version_requirements("maf-sandbox-bicep")


def _ok(_core_wheel: Path, _distribution: str, _version: str) -> str | None:
    return None


def _breaks_docker_020(_core_wheel: Path, distribution: str, version_str: str) -> str | None:
    # Breaks only the old admitting version; any other (distribution, version) imports clean.
    if distribution == "maf-sandbox-docker" and version_str == "0.2.0":
        return "ImportError: cannot import name 'CallerContext' from 'maf_sandbox'"
    return None


def _fake_fetch_with_docker_020(distribution: str) -> dict[str, list[str]]:
    """Every dependent admits 0.11.0 at a `<0.13` latest; docker also has an old 0.2.0 at `<0.12`."""
    if distribution == "maf-sandbox-docker":
        return {
            "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
            "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
        }
    return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}


# The full admitting set build tested under `_fake_fetch_with_docker_020`: every dependent's 0.6.0
# plus docker's old 0.2.0, sorted by distribution then version. The build snapshot the upload diff
# reads back, and the expected content of an `--emit-snapshot` run.
_ADMITTING_AT_BUILD: list[tuple[str, str]] = [
    ("maf-sandbox-acas", "0.6.0"),
    ("maf-sandbox-bicep", "0.6.0"),
    ("maf-sandbox-codeact", "0.6.0"),
    ("maf-sandbox-docker", "0.2.0"),
    ("maf-sandbox-docker", "0.6.0"),
    ("maf-sandbox-wslc", "0.6.0"),
]


class TestBreaks:
    """The verdict over an injected install/import, so the decision is testable offline."""

    def test_every_import_clean_is_no_failure(self):
        candidates = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        assert check.breaks(Path("core.whl"), candidates, _ok) == []

    def test_a_break_is_named_with_its_version_and_reason(self):
        candidates = [("maf-sandbox-docker", "0.2.0"), ("maf-sandbox-docker", "0.6.0")]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_docker_020)
        assert failures == [
            "maf-sandbox-docker==0.2.0: ImportError: cannot import name 'CallerContext' "
            "from 'maf_sandbox'"
        ]

    def test_only_the_broken_version_is_reported(self):
        candidates = [("maf-sandbox-docker", "0.2.0"), ("maf-sandbox-docker", "0.6.0")]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_docker_020)
        assert len(failures) == 1
        assert "0.6.0" not in failures[0]


class TestNewlyAdmitting:
    """The diff core: current admitting versions minus the snapshot build tested."""

    def test_everything_already_tested_yields_nothing(self):
        current = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        snapshot = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        assert check.newly_admitting(current, snapshot) == []

    def test_a_version_not_in_the_snapshot_is_new(self):
        current = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.7.0")]
        snapshot = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        # 0.7.0 is the newly uploaded non-latest admitting version — the race #284 closes.
        assert check.newly_admitting(current, snapshot) == [("maf-sandbox-docker", "0.7.0")]

    def test_a_version_gone_from_current_is_ignored(self):
        # A version build tested that is no longer admitting (yanked again, or excluded) is not a
        # risk to re-test, so it is simply absent from the result — not an error, not re-tested.
        current = [("maf-sandbox-bicep", "0.5.6")]
        snapshot = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        assert check.newly_admitting(current, snapshot) == []

    def test_current_order_is_preserved(self):
        current = [
            ("maf-sandbox-acas", "0.5.0"),
            ("maf-sandbox-bicep", "0.5.6"),
            ("maf-sandbox-docker", "0.7.0"),
        ]
        snapshot = [("maf-sandbox-bicep", "0.5.6")]
        assert check.newly_admitting(current, snapshot) == [
            ("maf-sandbox-acas", "0.5.0"),
            ("maf-sandbox-docker", "0.7.0"),
        ]


class TestSnapshotIO:
    """``write_snapshot`` / ``read_snapshot`` round-trip the tested pairs, and fail closed."""

    def test_a_round_trip_preserves_the_pairs(self, tmp_path: Path):
        path = tmp_path / "snap.json"
        pairs = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        check.write_snapshot(path, pairs)
        assert check.read_snapshot(path) == pairs

    def test_the_file_is_a_json_list_of_pairs(self, tmp_path: Path):
        path = tmp_path / "snap.json"
        check.write_snapshot(path, [("maf-sandbox-bicep", "0.5.6")])
        assert json.loads(path.read_text()) == [["maf-sandbox-bicep", "0.5.6"]]

    def test_a_missing_snapshot_fails_closed(self, tmp_path: Path):
        with pytest.raises(ValueError, match="snapshot not found"):
            check.read_snapshot(tmp_path / "no-such.json")

    def test_malformed_json_fails_closed(self, tmp_path: Path):
        path = tmp_path / "snap.json"
        path.write_text("not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            check.read_snapshot(path)

    def test_a_non_list_snapshot_fails_closed(self, tmp_path: Path):
        path = tmp_path / "snap.json"
        path.write_text(json.dumps({"maf-sandbox-bicep": "0.5.6"}))
        with pytest.raises(ValueError, match="not a list"):
            check.read_snapshot(path)

    def test_an_entry_that_is_not_a_pair_fails_closed(self, tmp_path: Path):
        path = tmp_path / "snap.json"
        path.write_text(json.dumps([["maf-sandbox-bicep", "0.5.6"], ["solo"]]))
        with pytest.raises(ValueError, match="not a list"):
            check.read_snapshot(path)


class _CompletedProcess:
    """The slice of ``subprocess.CompletedProcess`` the script reads: returncode, stdout, stderr."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestInstallAndImport:
    """The subprocess sequence that builds the venv, installs, and imports.

    The verdict tests above pass a fake for the whole step; these pin the argv the real one builds
    and the branch each failure returns, by mocking ``subprocess.run``. No network and no real venv
    — uv is never invoked.
    """

    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        results: list[_CompletedProcess],
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> _CompletedProcess:
            calls.append(cmd)
            return results[len(calls) - 1]

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        return calls

    def test_the_success_sequence_pins_the_version_and_imports(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        calls = self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(0), _CompletedProcess(0)],
        )
        assert check.install_and_import(tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6") is None
        assert len(calls) == 3
        assert calls[0][:2] == ["uv", "venv"]
        assert calls[1][:4] == ["uv", "pip", "install", "--python"]
        assert str(tmp_path / "core.whl") in calls[1]
        assert "maf-sandbox-bicep==0.5.6" in calls[1]
        assert calls[2][1] == "-c"
        assert "import maf_sandbox_bicep" in calls[2]

    def test_a_venv_creation_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        self._patch(monkeypatch, [_CompletedProcess(1, stderr="uv: boom")])
        error = check.install_and_import(tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6")
        assert error == "uv venv failed: uv: boom"

    def test_an_install_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(1, stderr="No solution found")],
        )
        error = check.install_and_import(tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6")
        assert error == "install failed: No solution found"

    def test_an_import_failure_reports_the_last_traceback_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ImportError: cannot import name 'CallerContext'\n"
        )
        self._patch(
            monkeypatch,
            [
                _CompletedProcess(0),
                _CompletedProcess(0),
                _CompletedProcess(1, stderr=stderr),
            ],
        )
        error = check.install_and_import(tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6")
        assert error == "ImportError: cannot import name 'CallerContext'"

    def test_an_import_failure_with_no_output_still_names_the_module(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(0), _CompletedProcess(1)],
        )
        error = check.install_and_import(tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6")
        assert error == "import maf_sandbox_bicep failed"


class TestMain:
    """The wiring — fetch, filter, verify, exit code — with both impure steps faked."""

    def _wheel(self, tmp_path: Path) -> Path:
        wheel = tmp_path / "maf_sandbox-0.11.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        return wheel

    def test_the_wrong_number_of_arguments(self, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_too_many_arguments_with_a_flag_is_still_wrong(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(tmp_path / "snap.json"),
                    "extra",
                ]
            )
            == 2
        )
        assert "usage:" in capsys.readouterr().err

    def test_a_flag_without_its_path_argument_is_wrong(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path)), "--emit-snapshot"]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_emit_and_since_snapshot_together_are_wrong(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        snap = tmp_path / "snap.json"
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--emit-snapshot",
                    str(snap),
                    "--since-snapshot",
                    str(snap),
                ]
            )
            == 2
        )
        assert "usage:" in capsys.readouterr().err

    def test_a_missing_core_wheel(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0, "0.11.0", str(tmp_path / "no-such.whl")]) == 1
        assert "no core wheel" in capsys.readouterr().err

    def test_all_versions_with_a_broken_old_version_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # The old admitting version breaks and is reported; the current admitting version imports
        # clean and is not. Both admit, so only the all-versions filter surfaces the breaking one.
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)
        monkeypatch.setattr(check, "install_and_import", _breaks_docker_020)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 1
        captured = capsys.readouterr()
        assert "maf-sandbox-docker==0.2.0: ImportError" in captured.err
        assert "Release order" in captured.err
        # A break refuses the release before the dispatch is decided, so it prints no verdict.
        assert "live_check=" not in captured.out

    def test_all_versions_all_clean_names_every_admitting_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "imports against it" in out
        assert "maf-sandbox-docker==0.2.0" in out
        assert "live_check=run" in out

    def test_every_version_excluded_leaves_nothing_to_verify(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # every published version excludes the candidate — the admit check's refusal, not a pass
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.5.0": ["maf-sandbox<0.11,>=0.10.0"]},
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "nothing to verify" in out
        assert "live_check=skip" in out

    def test_emit_snapshot_records_the_admitting_versions_build_tested(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.setattr(check, "fetch_version_requirements", _fake_fetch_with_docker_020)
        monkeypatch.setattr(check, "install_and_import", _ok)
        snap = tmp_path / "snap.json"
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--emit-snapshot",
                    str(snap),
                ]
            )
            == 0
        )
        # The snapshot is the full admitting set build tested, as sorted (distribution, version)
        # pairs: every dependent's 0.6.0 plus docker's old 0.2.0.
        assert json.loads(snap.read_text()) == [[d, v] for d, v in _ADMITTING_AT_BUILD]
        # The build run emits its provisional verdict here alongside the snapshot: every admitting
        # dependent imported, so the provisional reading is `run`. The dispatch verdict is the
        # upload-time re-check's, not this one (#337).
        assert "live_check=run" in capsys.readouterr().out

    def test_emit_snapshot_is_written_even_when_a_break_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Build refuses on a break, but the snapshot of what it tested is still on disk — only
        # relevant if a later step reads it, and the workflow only uploads it on build success.
        monkeypatch.setattr(check, "fetch_version_requirements", _fake_fetch_with_docker_020)
        monkeypatch.setattr(check, "install_and_import", _breaks_docker_020)
        snap = tmp_path / "snap.json"
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--emit-snapshot",
                    str(snap),
                ]
            )
            == 1
        )
        assert snap.exists()
        assert ["maf-sandbox-docker", "0.2.0"] in json.loads(snap.read_text())

    def test_since_snapshot_with_nothing_new_installs_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # The common case: the admitting set at upload matches what build tested, so the diff is
        # empty and install_and_import is never called.
        monkeypatch.setattr(check, "fetch_version_requirements", _fake_fetch_with_docker_020)

        calls: list[tuple[str, str]] = []

        def _spy(_wheel: Path, distribution: str, version: str) -> str | None:
            calls.append((distribution, version))
            return None

        monkeypatch.setattr(check, "install_and_import", _spy)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, _ADMITTING_AT_BUILD)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                ]
            )
            == 0
        )
        assert calls == []
        out = capsys.readouterr().out
        assert "nothing to re-verify" in out
        # The re-check emits the dispatch verdict: the admitting set is unchanged since build, and
        # a published wheel is immutable, so the versions build tested still import — run.
        assert "live_check=run" in out

    def test_since_snapshot_retests_a_newly_admitting_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # A newly uploaded non-latest admitting version (docker 0.7.0) is not in the build snapshot
        # and is re-tested; the versions build already tested are not.
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                    "0.7.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)
        monkeypatch.setattr(check, "install_and_import", _ok)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, _ADMITTING_AT_BUILD)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "maf-sandbox-docker==0.7.0" in out
        assert "0.2.0" not in out
        assert "live_check=run" in out

    def test_since_snapshot_refuses_when_a_newly_admitting_version_breaks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                    "0.7.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)

        def _breaks_070(_wheel: Path, distribution: str, version_str: str) -> str | None:
            if distribution == "maf-sandbox-docker" and version_str == "0.7.0":
                return "ImportError: cannot import name 'CallerContext'"
            return None

        monkeypatch.setattr(check, "install_and_import", _breaks_070)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, _ADMITTING_AT_BUILD)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                ]
            )
            == 1
        )
        captured = capsys.readouterr()
        assert "maf-sandbox-docker==0.7.0: ImportError" in captured.err
        assert "Release order" in captured.err
        # A break refuses the upload before the dispatch is decided, so no verdict is printed.
        assert "live_check=" not in captured.out

    def test_since_snapshot_still_skips_when_nothing_admits_at_upload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Build found nothing admitting either (empty snapshot), and nothing admitted in the
        # approval window, so the upload-time verdict is still skip — the #273 window held.
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.6.0": ["maf-sandbox<0.11,>=0.10.0"]},  # ceiling excludes 0.11.0
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, [])  # build tested nothing
        assert (
            check.main(
                [_ARGV0, "0.11.0", str(self._wheel(tmp_path)), "--since-snapshot", str(snap)]
            )
            == 0
        )
        assert "live_check=skip" in capsys.readouterr().out

    def test_since_snapshot_recovers_when_a_dependent_admits_in_the_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Build found nothing admitting and would have emitted `skip` (empty snapshot). A
        # dependent then uploaded an admitting version during the approval window, so the
        # upload-time re-check sees it, re-tests it, and the verdict flips to run — the stale-skip
        # case the upload-time dispatch exists to fix (#337).
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.6.0": ["maf-sandbox<0.13,>=0.11.0"]},  # admits 0.11.0
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, [])  # build tested nothing
        assert (
            check.main(
                [_ARGV0, "0.11.0", str(self._wheel(tmp_path)), "--since-snapshot", str(snap)]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "maf-sandbox-acas==0.6.0" in out  # newly admitting, re-tested
        assert "live_check=run" in out

    def test_since_snapshot_with_a_missing_snapshot_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]},
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(tmp_path / "no-such.json"),
                ]
            )
            == 1
        )
        assert "snapshot not found" in capsys.readouterr().err

    def test_dispatch_with_a_newly_admitting_break_emits_run_and_does_not_refuse(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Once the upload is immutable, a newly discovered break must dispatch instead of refusing.
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                    "0.7.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)

        def _breaks_070(_wheel: Path, distribution: str, version_str: str) -> str | None:
            if distribution == "maf-sandbox-docker" and version_str == "0.7.0":
                return "ImportError: cannot import name 'CallerContext'"
            return None

        monkeypatch.setattr(check, "install_and_import", _breaks_070)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, _ADMITTING_AT_BUILD)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                    "--dispatch",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        assert "live_check=run" in captured.out
        assert "maf-sandbox-docker==0.7.0: ImportError" in captured.err
        # The refusal prose belongs to the pre-upload guard, not the post-upload dispatch.
        assert "Release order" not in captured.err
        assert "#443" in captured.err or "443" in captured.err

    def test_dispatch_with_nothing_new_emits_run_installing_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # `--dispatch` does not change the common case: the admitting set at upload matches what
        # build tested, the diff is empty, install_and_import is never called, and the verdict is
        # run — the build-tested versions are immutable and still import.
        monkeypatch.setattr(check, "fetch_version_requirements", _fake_fetch_with_docker_020)

        calls: list[tuple[str, str]] = []

        def _spy(_wheel: Path, distribution: str, version: str) -> str | None:
            calls.append((distribution, version))
            return None

        monkeypatch.setattr(check, "install_and_import", _spy)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, _ADMITTING_AT_BUILD)
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                    "--dispatch",
                ]
            )
            == 0
        )
        assert calls == []
        assert "live_check=run" in capsys.readouterr().out

    def test_dispatch_with_nothing_admitting_emits_skip(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # The skip path is unaffected by `--dispatch`: nothing admits after the upload, so the
        # live check is skipped — the #273 window held through the upload.
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.6.0": ["maf-sandbox<0.11,>=0.10.0"]},  # ceiling excludes 0.11.0
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        snap = tmp_path / "snap.json"
        check.write_snapshot(snap, [])  # build tested nothing
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--since-snapshot",
                    str(snap),
                    "--dispatch",
                ]
            )
            == 0
        )
        assert "live_check=skip" in capsys.readouterr().out

    def test_dispatch_with_emit_snapshot_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # `--dispatch` derives the verdict after the upload; `--emit-snapshot` records what the
        # build run tested. The two belong to different runs, so combining them is a misuse.
        snap = tmp_path / "snap.json"
        assert (
            check.main(
                [
                    _ARGV0,
                    "0.11.0",
                    str(self._wheel(tmp_path)),
                    "--emit-snapshot",
                    str(snap),
                    "--dispatch",
                ]
            )
            == 2
        )
        assert "usage:" in capsys.readouterr().err
