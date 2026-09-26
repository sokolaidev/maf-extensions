"""Require both policy responses and independent origin receipts in ACAS evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probe_acas_egress import measure_adapter, policies, verify_case  # noqa: E402

HOSTS = ["a.probe.example", "b.probe.example"]


def _case():
    return {
        "name": "get",
        "requests": [
            {
                "id": "get",
                "method": "GET",
                "url": "https://a.probe.example/probe?id=get",
                "follow": False,
            },
            {
                "id": "post",
                "method": "POST",
                "url": "https://a.probe.example/probe?id=post",
                "follow": False,
            },
        ],
        "results": [
            {"id": "get", "hops": [{"method": "GET", "status": 200, "denial": None}]},
            {
                "id": "post",
                "hops": [{"method": "POST", "status": 403, "denial": "a.probe.example:POST"}],
            },
        ],
        "receipts": [{"id": "get", "method": "GET", "origin": "0"}],
    }


def test_allowed_control_and_denied_method_have_independent_origin_evidence():
    assert verify_case(_case(), policies(HOSTS)["get"], HOSTS) == 2


@pytest.mark.parametrize(
    "damage",
    [
        "missing-hit",
        "denied-hit",
        "widened",
        "origin-403",
        "transport-error",
        "missing-result",
        "duplicate-result",
    ],
)
def test_incomplete_or_contradictory_evidence_cannot_pass(damage):
    case = copy.deepcopy(_case())
    if damage == "missing-hit":
        case["receipts"] = []
    elif damage == "denied-hit":
        case["receipts"].append({"id": "post", "method": "POST", "origin": "0"})
    elif damage == "widened":
        case["results"][1]["hops"][0]["status"] = 200
    elif damage == "origin-403":
        case["results"][1]["hops"][0]["denial"] = None
    elif damage == "transport-error":
        case["results"][0]["error"] = "ConnectionResetError"
    elif damage == "missing-result":
        case["results"].pop()
    else:
        case["results"][1] = copy.deepcopy(case["results"][0])
    with pytest.raises(AssertionError):
        verify_case(case, policies(HOSTS)["get"], HOSTS)


@pytest.mark.skipif(
    not os.environ.get("MAF_ACAS_EGRESS_PROBE_CONFIG"),
    reason="requires two recording origins and an ACAS group",
)
def test_qualified_policies_through_the_live_adapter(tmp_path):
    config = json.loads(
        Path(os.environ["MAF_ACAS_EGRESS_PROBE_CONFIG"]).read_text(encoding="utf-8-sig")
    )
    output = tmp_path / "adapter-report.json"
    asyncio.run(measure_adapter(config, output, set()))
    report = json.loads(output.read_text())
    assert report["cleanup"] and report["shared_conformance"]
    assert all(case["verified_https_hops"] > 0 for case in report["cases"])
