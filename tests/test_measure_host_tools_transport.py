"""Keep transport measurements structural rather than timing-based."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_host_tools_transport.py"
sys.path.insert(0, str(_SCRIPT.parent))

import measure_host_tools_transport as measurement  # noqa: E402


def test_sequential_and_concurrent_guest_publication_are_measured_without_timing_gates():
    sequential, concurrent = measurement.measure()

    assert sequential.mode == "sequential"
    assert concurrent.mode == "concurrent"
    assert sequential.answers == concurrent.answers == tuple(range(1, 9))
    assert sequential.dispatches == concurrent.dispatches == 8
    assert sequential.host_arrivals == concurrent.host_arrivals == 8

    # A+B's speculative discovery changes probes, not the ordered host-tool contract. Keep the
    # counts visible and comparable, but do not assert absolute values that can change with tuning.
    assert sequential.stat_probes > 0
    assert concurrent.stat_probes > 0
    assert sequential.read_probes == concurrent.read_probes
    assert sequential.write_probes == concurrent.write_probes
    assert len(sequential.host_arrival_gaps) == len(concurrent.host_arrival_gaps) == 7
    assert all(gap >= 0 for gap in sequential.host_arrival_gaps + concurrent.host_arrival_gaps)
