"""ACAS live suites require complete configuration before enabling their cases."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

SETTINGS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
    "ACAS_SANDBOX_REGISTRY",
)
SUITES = [
    ("test_opentofu_platform_offline.py", "MAF_OPENTOFU_PLATFORM_ACAS_IMAGE"),
    ("test_terraform_avm_offline.py", "MAF_TERRAFORM_AVM_ACAS_IMAGE"),
]


@pytest.mark.parametrize("filename,image_variable", SUITES)
@pytest.mark.parametrize("missing", [*SETTINGS, "image", None])
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_acas_cases_require_every_setting(
    filename, image_variable, missing, value, tmp_path, monkeypatch
):
    for name in (*SETTINGS, image_variable):
        monkeypatch.setenv(name, "configured")
    (tmp_path / "receipt.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MAF_TERRAFORM_AVM_DIR", str(tmp_path))
    if missing is not None:
        name = image_variable if missing == "image" else missing
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    suite = runpy.run_path(str(Path(__file__).with_name(filename)))
    acas = next(case for case in suite["BACKENDS"] if case.values == ("acas",))
    skip = next(mark for mark in acas.marks if mark.name == "skipif")
    assert skip.args[0] is (missing is not None)
