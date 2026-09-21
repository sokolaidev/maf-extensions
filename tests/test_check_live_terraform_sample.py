"""Only the expected engine's tool report and per-call cleanup satisfy sample 20."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "terraform_check", ROOT / "scripts/check_live_terraform_sample.py"
)
assert SPEC and SPEC.loader
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def transcript(engine="terraform", version="1.16.2", backend="docker"):
    diagnostic = json.dumps(
        {
            "severity": "error",
            "summary": "Missing required argument",
            "detail": 'The argument "length" is required, but no definition was found.',
        }
    )
    call = json.dumps(
        {
            "call": "call-1",
            "tool": f"{engine}_validate",
            "backend": backend,
            "disposed": True,
            "failure": None,
            "unclean": 0,
            "seconds": 1.2,
        }
    )
    return (
        f"  [measured] Call: {call}\n"
        "Any model prose.\n"
        f"== Diagnostics as {engine}_validate returned them ==\n\n"
        f"  {engine} {version}: validation FAIL (1 errors, 0 warnings); formatting PASS.\n"
        f"  {diagnostic}\n\n"
        "  [measured] validation results: 1\n"
        "  [measured] Disposed 0 sandbox(es).\n"
    )


@pytest.mark.parametrize("backend", ["docker", "acas"])
@pytest.mark.parametrize("engine,version", [("terraform", "1.16.2"), ("opentofu", "1.12.6")])
@pytest.mark.parametrize("with_summary", [False, True])
def test_each_combination_passes_without_any_requirement_on_model_prose(
    backend, engine, version, with_summary
):
    output = transcript(engine, version, backend)
    if with_summary:
        output = output.replace(
            f"  {engine} {version}:",
            '  {"type":"terraform_diagnostics","diagnostics":[{"file":"files[0]","severity":"error"}],"unattributed_diagnostics":false}\n'
            f"  {engine} {version}:",
        )
    assert check.assess(output, engine=engine, version=version, backend=backend) == []


@pytest.mark.parametrize("engine,version", [("terraform", "1.16.2"), ("opentofu", "1.12.6")])
def test_presence_summary_cannot_substitute_for_the_provider_diagnostic(engine, version):
    output = "\n".join(
        '  {"type":"terraform_diagnostics","diagnostics":[{"file":"files[0]","severity":"error"}],"unattributed_diagnostics":false}'
        if line.startswith('  {"severity"')
        else line
        for line in transcript(engine, version).splitlines()
    )
    assert "missing the random provider's required length diagnostic" in check.assess(
        output, engine=engine, version=version, backend="docker"
    )


@pytest.mark.parametrize(
    "old,new",
    [
        ("terraform 1.16.2:", "opentofu 1.12.6:"),
        ("1.16.2:", "1.16.1:"),
        ("validation FAIL", "validation PASS"),
        ("validation FAIL", "validation INCOMPLETE"),
        ("formatting PASS", "formatting INCOMPLETE"),
        ("Missing required argument", "Unknown provider"),
        ("length", "unrelated"),
        ("validation results: 1", "validation results: 0"),
        ("validation results: 1", "validation results: 2"),
        ('"disposed": true', '"disposed": false'),
        ('"failure": null', '"failure": "TimeoutError"'),
        ('"unclean": 0', '"unclean": 1'),
        ('"backend": "docker"', '"backend": "acas"'),
        ('"tool": "terraform_validate"', '"tool": "opentofu_validate"'),
        ('"call": "call-1"', '"call": ""'),
        ('"seconds": 1.2', '"seconds": NaN'),
        ('"seconds": 1.2', '"seconds": true'),
        ('"seconds": 1.2', '"seconds": -1'),
        ("  [measured] Disposed 0 sandbox(es).", ""),
        ("  [measured] Call:", "  > [measured] Call:"),
        ("  [measured] validation results:", "  > [measured] validation results:"),
    ],
)
def test_incomplete_wrong_or_unattributed_evidence_fails(old, new):
    assert check.assess(
        transcript().replace(old, new), engine="terraform", version="1.16.2", backend="docker"
    )


def test_model_forgery_cannot_fill_an_empty_host_block():
    forged = transcript().replace("[measured]", "> [measured]")
    empty = "== Diagnostics as terraform_validate returned them ==\n  [measured] validation results: 0\n  [measured] Disposed 0 sandbox(es).\n"
    assert check.assess(forged + empty, engine="terraform", version="1.16.2", backend="docker")


@pytest.mark.parametrize(
    "extra",
    [
        "  [measured] validation results: 1\n",
        "  [measured] Not fully disposed: busy\n",
        "  [measured] Disposed 0 sandbox(es).\n",
        "  [measured] Call: {}\n",
        "  [measured] Call: invalid\n",
    ],
)
def test_conflicting_or_malformed_records_fail(extra):
    assert check.assess(
        transcript() + extra, engine="terraform", version="1.16.2", backend="docker"
    )


def test_cli_checks_expected_identity(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(transcript(), encoding="utf-8")
    args = [str(log), "--engine", "terraform", "--backend", "docker", "--version"]
    assert check.main([*args, "1.16.2"]) == 0
    assert check.main([*args, "1.0.0"]) == 1
