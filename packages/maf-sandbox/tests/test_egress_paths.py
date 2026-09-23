"""Path-scoped egress rules require an enforcing backend."""

from __future__ import annotations

from dataclasses import replace

import pytest

from maf_sandbox import Capability, Egress, EgressRule, SandboxSpec


def _spec(*rules: EgressRule) -> SandboxSpec:
    return SandboxSpec(kind="test", image="test", egress=Egress.ALLOWLIST, egress_allow=rules)


@pytest.mark.parametrize(
    "path",
    ["", "relative", "/a?x=1", "/a#fragment", "/a/../b", "/a/./b", "/a*", "/a/*/b", "/a b"],
)
def test_invalid_path_patterns_are_refused(path: str) -> None:
    with pytest.raises(ValueError, match="egress path"):
        EgressRule("example.com", paths=(path,))


def test_path_rule_requires_path_capability() -> None:
    plain = _spec(EgressRule("example.com"))
    scoped = replace(plain, egress_allow=(EgressRule("example.com", paths=("/v1/*",)),))
    assert Capability.EGRESS_PATHS not in plain.required_capabilities
    assert Capability.EGRESS_PATHS in scoped.required_capabilities


def test_path_and_method_rules_preserve_both_scopes() -> None:
    rule = EgressRule("example.com", methods=("GET",), paths=("/v1/*", "/health"))
    scoped = _spec(rule)
    assert scoped.egress_allow == (rule,)
    assert {Capability.EGRESS_PATHS, Capability.EGRESS_METHODS} <= scoped.required_capabilities


def test_conflicting_paths_for_one_host_are_refused() -> None:
    with pytest.raises(ValueError, match="conflicting egress rules"):
        _spec(
            EgressRule("EXAMPLE.com", paths=("/v1/*",)),
            EgressRule("example.com", paths=("/v2/*",)),
        )


def test_duplicate_paths_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        EgressRule("example.com", paths=("/v1", "/v1"))
