"""Actual-result and cleanup evidence cannot be replaced with model-authored prose."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
checker_spec = importlib.util.spec_from_file_location(
    "check_live_backend_pricing", ROOT / "scripts/check_live_backend_pricing.py"
)
assert checker_spec and checker_spec.loader
checker = importlib.util.module_from_spec(checker_spec)
checker_spec.loader.exec_module(checker)
PREFIX = checker.PREFIX
check = checker.check


def records():
    rows = [
        {
            "stage": "identity",
            "backend": "bubblewrap",
            "source": "a" * 40,
            "runtime": "sha256:" + "b" * 64,
            "proxy_sha256": "c" * 64,
            "versions": {"maf-sandbox": "0.53.0"},
        }
    ]
    for index, category in enumerate(("Consumption", "Reservation")):
        call, instance = f"call-{index}", f"instance-{index}"
        price = {
            "retailPrice": 1.25,
            "currencyCode": "USD",
            "unitOfMeasure": "1 Hour",
            "priceType": category,
            "reservationTerm": "1 Year",
            "armSkuName": "Standard_D4s_v5",
            "region": "westeurope",
        }
        rows.extend(
            [
                {"stage": "acquire", "call": call, "instance": instance},
                {"stage": "call", "call": call, "seconds": 1.2, "unclean": 0, "failure": None},
                {
                    "stage": "report",
                    "call": call,
                    "seconds": 1.3,
                    "report": {"prices": [price], "truncated": False},
                },
                {"stage": "disposed", "instance": instance, "complete": True},
            ]
        )
    rows.append({"stage": "cleanup", "complete": True, "calls": 2, "model": False})
    return rows


def log(rows):
    return "\n".join(PREFIX + json.dumps(row) for row in rows)


def test_complete_tool_only_evidence_is_explicitly_not_model_qualification():
    assert check(log(records()), "bubblewrap", model=False)["calls"] == 2
    with pytest.raises(ValueError):
        check(log(records()), "bubblewrap")


@pytest.mark.parametrize(
    "failure",
    [
        "prose",
        "missing_cleanup",
        "missing_report",
        "reused_instance",
        "wrong_call",
        "unclean",
        "bad_time",
        "wrong_units",
        "wrong_sku",
        "missing_term",
        "truncated",
        "empty",
    ],
)
def test_evidence_refuses_incomplete_or_misattributed_results(failure):
    rows = copy.deepcopy(records())
    if failure == "prose":
        text = "The model says the price is 1.25 USD and cleanup succeeded."
    else:
        if failure == "missing_cleanup":
            rows.pop()
        elif failure == "missing_report":
            rows.pop(3)
        elif failure == "reused_instance":
            rows[5]["instance"] = rows[1]["instance"]
        elif failure == "wrong_call":
            rows[3]["call"] = "unrelated"
        elif failure == "unclean":
            rows[2]["unclean"] = 1
        elif failure == "bad_time":
            rows[2]["seconds"] = float("nan")
        elif failure == "wrong_units":
            rows[3]["report"]["prices"][0]["unitOfMeasure"] = ""
        elif failure == "wrong_sku":
            rows[3]["report"]["prices"][0]["armSkuName"] = "different"
        elif failure == "missing_term":
            del rows[7]["report"]["prices"][0]["reservationTerm"]
        elif failure == "truncated":
            rows[3]["report"]["truncated"] = True
        elif failure == "empty":
            rows[3]["report"]["prices"] = []
        text = log(rows)
    with pytest.raises(ValueError):
        check(text, "bubblewrap", model=False)


def load_sample():
    spec = importlib.util.spec_from_file_location(
        "backend_pricing_sample", ROOT / "samples/experimental/azure_pricing/agent.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_preserves_empty_and_truncated_tool_reports():
    from agent_framework import Content
    from maf_sandbox.maf import COMPLETED_TEXT

    sample = load_sample()
    value = {"prices": [], "truncated": True}
    answer = [Content.from_text(COMPLETED_TEXT), Content.from_text(json.dumps(value))]
    assert sample.report(answer) == value
    with pytest.raises(ValueError, match="did not complete"):
        sample.report([Content.from_text("The model claims there are prices")])


@pytest.mark.skipif(os.environ.get("MAF_PRICING_E2E") != "1", reason="opt-in real pricing host")
def test_live_backend_pricing():
    args = json.loads(os.environ["MAF_PRICING_LIVE_ARGS"])
    assert isinstance(args, list) and all(isinstance(item, str) for item in args)
    backend = args[args.index("--backend") + 1]
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "samples/experimental/azure_pricing/agent.py"),
            *args,
            "--no-model",
            "--qualify",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    Path(os.environ["MAF_PRICING_EVIDENCE_LOG"]).write_text(
        result.stdout + "\n" + result.stderr, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    check(result.stdout, backend, model=False, require_probes=True)
