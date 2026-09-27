"""Operator policy must freshly verify every candidate before emitting admission rules."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_hyperlight_admission as admission

pytestmark = pytest.mark.workflow


def candidate(digit: str = "a") -> dict[str, str]:
    return {
        "image": f"registry.example/runtime@sha256:{digit * 64}",
        "signer_identity": "https://github.com/sokolaidev/maf-extensions/.github/workflows/build.yml@refs/heads/main",
        "source_revision": "b" * 40,
        "source_ref": "refs/heads/main",
        "build_inputs_sha256": "c" * 64,
    }


def write_policy(tmp_path: Path, **overrides: Any) -> Path:
    policy = {"namespace": "runtime-apps", "candidates": [candidate()], **overrides}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_fresh_verification_and_exact_namespace_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def verify(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return {"fresh": kwargs["image"]}

    monkeypatch.setattr(admission, "verify_published_image", verify)
    candidates = [candidate(), candidate("d")]
    policy = write_policy(tmp_path, candidates=candidates)
    output = tmp_path / "promotion.json"
    output.write_text('{"signed_provenance_verified":true}', encoding="utf-8")
    bundle = admission.prepare(policy, output)
    assert [call["image"] for call in calls] == [c["image"] for c in candidates]
    assert bundle == json.loads(output.read_text(encoding="utf-8"))
    vap, binding = bundle["admission"]["items"]  # type: ignore[index]
    spec = vap["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["matchConditions"] == [
        {
            "name": "runtime-namespace",
            "expression": 'request.namespace == "runtime-apps"',
        }
    ]
    assert "objectSelector" not in spec["matchConstraints"]
    assert "namespaceSelector" not in spec["matchConstraints"]
    assert spec["matchConstraints"]["resourceRules"][0]["resources"] == [
        "pods",
        "pods/ephemeralcontainers",
    ]
    assert binding["spec"]["validationActions"] == ["Deny"]
    expressions = [v["expression"] for v in spec["validations"]]
    for field in ("containers", "initContainers", "ephemeralContainers"):
        assert any(f"spec.{field}.all" in e for e in expressions)
    assert "!has(v.image)" in expressions[-1]
    for item in candidates:
        assert all(item["image"] in e for e in expressions[:3])


@pytest.mark.parametrize("failed_index", [0, 1])
def test_failed_candidate_removes_stale_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_index: int,
) -> None:
    calls = 0

    def verify(**kwargs: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == failed_index + 1:
            raise ValueError("refused")
        return {"image": kwargs["image"]}

    monkeypatch.setattr(admission, "verify_published_image", verify)
    policy = write_policy(tmp_path, candidates=[candidate(), candidate("d")])
    output = tmp_path / "promotion.json"
    output.write_text("stale approval", encoding="utf-8")
    with pytest.raises(ValueError, match="refused"):
        admission.prepare(policy, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"namespace": "kube-system"},
        {"namespace": 'x" || true'},
        {"namespace": ""},
        {"namespace": "x" * 64},
        {"candidates": []},
        {"candidates": [candidate()] * 9},
        {"candidates": [candidate(), candidate()]},
        {"candidates": [{**candidate(), "signed_provenance_verified": True}]},
        {"candidates": [{**candidate(), "source_ref": None}]},
        {"unexpected": True},
    ],
)
def test_invalid_policy_never_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    def verify(**kwargs: Any) -> dict[str, object]:
        pytest.fail("invalid policy must not start verification")

    monkeypatch.setattr(admission, "verify_published_image", verify)
    output = tmp_path / "promotion.json"
    output.write_text("stale", encoding="utf-8")
    with pytest.raises(ValueError):
        admission.prepare(write_policy(tmp_path, **overrides), output)
    assert not output.exists()


def test_policy_cannot_be_overwritten(tmp_path: Path) -> None:
    path = write_policy(tmp_path)
    with pytest.raises(ValueError, match="different files"):
        admission.prepare(path, path)
    assert path.exists()
