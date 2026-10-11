"""Installed-policy evidence must match the exact desired generation and runtime acknowledgement."""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_composition_probe")


def receipt_fixture():
    desired = NS(
        attachment_epoch="epoch-a",
        provider_env_revision=31,
        config_revision=17,
        policy_hash="hash-a",
    )
    receipt = NS(receipt_id="receipt-a", desired=desired)
    observed = NS(
        session_id="session-a",
        process_instance_id="process-a",
        **vars(desired),
        credentials_installed=True,
        policy_active=True,
        launch_environment_installed=True,
    )
    return receipt, NS(receipt=copy.deepcopy(receipt), state=3, observed=observed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("attachment_epoch", "epoch-b"),
        ("provider_env_revision", 32),
        ("config_revision", 18),
        ("policy_hash", "hash-b"),
        ("credentials_installed", False),
        ("policy_active", False),
        ("launch_environment_installed", False),
        ("session_id", ""),
        ("process_instance_id", ""),
    ],
)
def test_partial_or_different_installation_is_not_ready(probe, field, value):
    receipt, status = receipt_fixture()
    setattr(status.observed, field, value)
    assert not probe.receipt_installed(status, receipt, 3)


@pytest.mark.parametrize("state", [0, 1, 2, 4, 5, 6, 7])
def test_saved_pending_failed_or_superseded_receipt_is_not_ready(probe, state):
    receipt, status = receipt_fixture()
    status.state = state
    assert not probe.receipt_installed(status, receipt, 3)


def test_different_receipt_cannot_substitute_for_original(probe):
    receipt, status = receipt_fixture()
    status.receipt.receipt_id = "receipt-b"
    assert not probe.receipt_installed(status, receipt, 3)


def test_exact_installation_and_revocation_controls(probe):
    receipt, status = receipt_fixture()
    assert probe.receipt_installed(status, receipt, 3)
    status.state = 5
    assert probe.receipt_installed(status, receipt, 5)


@pytest.mark.parametrize("field,value", [("version", 3), ("policy_hash", "hash-b"), ("status", 1)])
def test_revision_requires_exact_loaded_version_and_hash(probe, field, value):
    response = NS(version=2, policy_hash="hash-a")
    status = NS(active_version=2, revision=NS(version=2, policy_hash="hash-a", status=2))
    setattr(status.revision, field, value)
    assert not probe.revision_installed(status, response, 2)


def test_historical_loaded_revision_is_not_current(probe):
    response = NS(version=2, policy_hash="hash-a")
    status = NS(active_version=3, revision=NS(version=2, policy_hash="hash-a", status=2))
    assert not probe.revision_installed(status, response, 2)
    status.active_version = 2
    assert probe.revision_installed(status, response, 2)
