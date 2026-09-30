"""An allowlist as ``sbx`` rules: what each global allow is to it, and what the plan sets."""

from __future__ import annotations

import json

import pytest
from maf_sandbox import EgressRule, HttpMethod

from maf_sandbox_docker_sbx._egress import (
    PostureRefused,
    classify,
    drift,
    plan_for,
    posture_refusal,
    requested,
)

GET_ONLY = EgressRule("pypi.org", methods=(HttpMethod.GET,))


@pytest.mark.parametrize(
    ("resource", "entry", "verdict"),
    [
        ("pypi.org:443", "pypi.org", "covered"),
        ("pypi.org", GET_ONLY, "overlap"),
        ("files.pythonhosted.org", "pypi.org", "disjoint"),
        ("**.openai.com:443", "*.openai.com", "covered"),
        ("**.openai.com", "*.com", "covered"),
        ("*.example.com", "*.example.com", "covered"),
        ("*.openai.com", "api.openai.com", "overlap"),
        ("**.github.com", "*.api.github.com", "overlap"),
        ("api?.example.com", "api1.example.com", "overlap"),
        ("api[12].example.com", "api3.example.com", "disjoint"),
        ("api[!1].example.com", "api2.example.com", "overlap"),
        ("api[1-3].example.com", "api2.example.com", "overlap"),
        ("api[!1-3].example.com", "api2.example.com", "disjoint"),
        ("api[a-].example.com", "api-.example.com", "overlap"),
        ("api[3-1].example.com", "api2.example.com", "overlap"),
        ("*.example.com", "*.other.com", "disjoint"),
        ("example.com", "*.example.com", "disjoint"),
        ("sub.example.com", "*.example.com", "covered"),
        ("10.0.0.0/8", "example.com", "disjoint"),
        ("[2001:db8::1]:443", "example.com", "disjoint"),
        ("**", "example.com", "overlap"),
        ("**:443", "example.com", "overlap"),
    ],
)
def test_a_global_allow_is_covered_overlapping_or_disjoint(resource, entry, verdict):
    assert classify(resource, requested((entry,))) == verdict


def test_the_plan_denies_the_disjoint_and_the_bare_name_under_a_wildcard():
    entries = requested(("pypi.org", "*.example.com", "*.test.io", "test.io"))
    plan = plan_for(entries, frozenset({"pypi.org:443", "npmjs.org", "**.example.com"}))
    assert plan.denies == ("npmjs.org", "example.com")
    assert plan.allows == (("pypi.org",), ("**.example.com",), ("**.test.io",), ("test.io",))


def test_an_overlapping_global_allow_refuses_the_plan():
    with pytest.raises(PostureRefused, match="'\\*\\*.github.com'"):
        plan_for(requested(("api.github.com",)), frozenset({"**.github.com"}))


def test_methods_and_paths_become_one_http_rule_per_path():
    rule = EgressRule("api.example.com", methods=("GET", "HEAD"), paths=("/v1/*", "/health"))
    assert plan_for(requested((rule,)), frozenset()).allows == (
        ("api.example.com", "--method", "GET,HEAD", "--path", "/v1/**"),
        ("api.example.com", "--method", "GET,HEAD", "--path", "/health"),
    )
    methods_only = EgressRule("api.example.com", methods=("POST",))
    assert plan_for(requested((methods_only,)), frozenset()).allows == (
        ("api.example.com", "--method", "POST", "--path", "/**"),
    )


def _secrets(**fields: object) -> bytes:
    return json.dumps({"secrets": [], "custom_secrets": [], "env_only_count": 0, **fields}).encode()


def test_a_service_secret_scoped_to_another_sandbox_does_not_refuse():
    entries = requested(("pypi.org",))
    other = _secrets(secrets=[{"scope": "sandbox:other", "name": "github"}])
    assert posture_refusal(entries, other, "mine", governed=False) is None
    mine = _secrets(secrets=[{"scope": "sandbox:mine", "name": "github"}])
    assert "github" in (posture_refusal(entries, mine, "mine", governed=False) or "")


def test_a_custom_secret_whose_range_covers_an_allowed_host_refuses():
    entries = requested(("api2.example.com",))
    ranged = _secrets(
        custom_secrets=[{"scope": "global", "targets": ["api[1-3].example.com"], "env": "K"}]
    )
    assert "'K'" in (posture_refusal(entries, ranged, "mine", governed=False) or "")


def test_an_inactive_sandbox_rule_is_drift():
    entries = requested(("pypi.org",))
    plan = plan_for(entries, frozenset())
    rules = [
        {
            "id": "r1",
            "scope": "sandbox:mine",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["pypi.org"],
            "status": "inactive",
        }
    ]
    policy = json.dumps({"rules": rules}).encode()
    assert "no longer active" in (drift(entries, plan, policy, "mine") or "")
    rules[0]["status"] = "active"
    assert drift(entries, plan, json.dumps({"rules": rules}).encode(), "mine") is None
