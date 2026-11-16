"""Accounting benchmark tooling preserves durable identity charges."""

import importlib
from pathlib import Path


def test_benchmark_preserves_identity_charges_and_separates_measurements(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    benchmark = importlib.import_module(
        "scripts.experiments.mxc_session_patch.accounting_benchmark"
    )
    for scanning in (False, True):
        result = benchmark.measure(tmp_path / str(scanning), 20, 3, scanning=scanning)
        assert result["retained_identities_after"] == 23
        assert result["samples"] == 3
        assert result["admission_median_seconds"] >= result["write_lock_median_seconds"] > 0
        assert result["accounting_audit_seconds"] > 0
        assert result["collection_zero_payload_seconds"] > 0
