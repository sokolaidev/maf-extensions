"""Operator policy must freshly verify every candidate before emitting admission rules."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_hyperlight_admission_live as live
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
        admission.sidecar_path(kwargs["output"]).write_bytes(kwargs["image"].encode())
        return {"fresh": kwargs["image"]}

    monkeypatch.setattr(admission, "verify_published_image", verify)
    candidates = [candidate(), candidate("d")]
    policy = write_policy(tmp_path, candidates=candidates)
    output = tmp_path / "promotion.json"
    output.write_text('{"signed_provenance_verified":true}', encoding="utf-8")
    bundle = admission.prepare(policy, output)
    assert [call["image"] for call in calls] == [c["image"] for c in candidates]
    assert bundle == json.loads(output.read_text(encoding="utf-8"))
    with zipfile.ZipFile(admission.sidecar_path(output)) as saved:
        assert saved.namelist() == [
            "candidate-0.json.evidence.zip",
            "candidate-1.json.evidence.zip",
        ]
        for index, item in enumerate(candidates):
            assert saved.read(f"candidate-{index}.json.evidence.zip") == item["image"].encode()
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
        admission.sidecar_path(kwargs["output"]).write_bytes(b"original bytes")
        return {"image": kwargs["image"]}

    monkeypatch.setattr(admission, "verify_published_image", verify)
    policy = write_policy(tmp_path, candidates=[candidate(), candidate("d")])
    output = tmp_path / "promotion.json"
    output.write_text("stale approval", encoding="utf-8")
    admission.sidecar_path(output).write_bytes(b"stale proof")
    with pytest.raises(ValueError, match="refused"):
        admission.prepare(policy, output)
    assert not output.exists()
    assert not admission.sidecar_path(output).exists()


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


@pytest.mark.parametrize("alias", ["same", "relative", "parent"])
def test_live_check_preserves_aliased_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    bundle = tmp_path / "promotion.json"
    contents = b'{"preserve": "signed promotion evidence"}\n'
    bundle.write_bytes(contents)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nested").mkdir()
    output = {
        "same": bundle,
        "relative": Path("promotion.json"),
        "parent": tmp_path / "nested" / ".." / "promotion.json",
    }[alias]

    def run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("aliased paths must be rejected before any cluster command")

    monkeypatch.setattr(live.subprocess, "run", run)
    try:
        with pytest.raises(ValueError, match="bundle and output must be different files"):
            live.check(bundle, output)
    finally:
        assert bundle.read_bytes() == contents


def test_live_check_removes_stale_report_for_distinct_invalid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "promotion.json"
    bundle.write_bytes(b"invalid JSON")
    output = tmp_path / "admission.json"
    output.write_text("stale success", encoding="utf-8")

    def run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid bundle must be rejected before any cluster command")

    monkeypatch.setattr(live.subprocess, "run", run)
    with pytest.raises(json.JSONDecodeError):
        live.check(bundle, output)
    assert bundle.read_bytes() == b"invalid JSON"
    assert not output.exists()
