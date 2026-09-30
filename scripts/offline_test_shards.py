"""Partition pytest's complete collection between repository and package CI jobs."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose the optional CI group without changing ordinary pytest runs."""
    parser.addoption(
        "--offline-group",
        choices=("repository", "packages"),
        help="run one half of the offline CI suite",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep each collected test in exactly one group after normal pytest selection."""
    group = config.getoption("offline_group")
    if group is None:
        return
    selected, deselected = [], []
    for item in items:
        in_package = item.nodeid.split("::", 1)[0].startswith("packages/")
        target = selected if in_package == (group == "packages") else deselected
        target.append(item)
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
