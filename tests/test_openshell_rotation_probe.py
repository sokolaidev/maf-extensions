"""A provider update acknowledgement must bind the intended sandbox, provider and revision."""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_rotation_probe")


def response_fixture():
    receipt = NS(
        receipt_id="receipt",
        mutation_id="mutation",
        provider="provider",
        desired=NS(sandbox_id="sandbox-id", provider_id="provider-id", provider_resource_version=4),
    )
    return NS(
        mutation_id="mutation",
        target_receipts=[receipt],
        provider=NS(metadata=NS(id="provider-id", resource_version=4)),
    )


@pytest.mark.parametrize("target_count", [0, 2])
def test_missing_or_ambiguous_target_set_is_refused(probe, target_count):
    response = response_fixture()
    response.target_receipts *= target_count
    with pytest.raises(RuntimeError):
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")


@pytest.mark.parametrize(
    "field,value", [("receipt_id", ""), ("mutation_id", "other"), ("provider", "other")]
)
def test_mismatched_receipt_is_refused(probe, field, value):
    response = response_fixture()
    setattr(response.target_receipts[0], field, value)
    with pytest.raises(RuntimeError):
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")


@pytest.mark.parametrize(
    "field,value",
    [("sandbox_id", "other"), ("provider_id", "other"), ("provider_resource_version", 3)],
)
def test_mismatched_target_identity_is_refused(probe, field, value):
    response = response_fixture()
    setattr(response.target_receipts[0].desired, field, value)
    with pytest.raises(RuntimeError):
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")


@pytest.mark.parametrize("field,value", [("id", "replacement"), ("resource_version", 0)])
def test_update_of_another_or_unversioned_provider_is_refused(probe, field, value):
    response = response_fixture()
    setattr(response.provider.metadata, field, value)
    with pytest.raises(RuntimeError):
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")


def test_missing_mutation_identity_is_refused(probe):
    response = response_fixture()
    response.mutation_id = ""
    with pytest.raises(RuntimeError):
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")


def test_exact_single_target_is_selected_without_mutation(probe):
    response = response_fixture()
    original = copy.deepcopy(response)
    assert (
        probe.select_update_receipt(response, "sandbox-id", "provider", "provider-id")
        is response.target_receipts[0]
    )
    assert response == original
