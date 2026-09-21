"""Upstream catalog extraction, policy preservation and update workflow boundaries."""

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_bicep_catalog as catalog


def source():
    return {
        catalog.SCHEMA: json.dumps(
            {
                "properties": {
                    "analyzers": {
                        "properties": {
                            "core": {
                                "properties": {
                                    "rules": {
                                        "properties": {
                                            "sample-rule": {
                                                "allOf": [
                                                    {
                                                        "$ref": "#/definitions/rule-def-level-warning"
                                                    },
                                                    {"properties": {"maxAge": {"default": 730}}},
                                                ]
                                            },
                                            "new-rule": {
                                                "allOf": [
                                                    {"$ref": "#/definitions/rule-def-level-off"}
                                                ]
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        ),
        catalog.COMPILER: '// "BCP999"\nCoreError("BCP033", "message"); /* "BCP888" */ CoreError("BCP001", "https://example.test/");',
        catalog.RULES + "Sample.cs": 'public const string Code = "sample-rule";',
        catalog.RULES + "New.cs": 'public new const string Code = "new-rule";',
    }


def test_generator_uses_explicit_codes_and_retains_local_rule_settings():
    existing = {
        "analyzers": {
            "core": {
                "rules": {
                    "sample-rule": {"level": "error", "maxAge": 365},
                    "removed-rule": {"level": "off"},
                }
            }
        }
    }
    snapshot = copy.deepcopy(existing)
    result = catalog.generate(source(), "v1.2.3", "a" * 40, existing)
    assert existing == snapshot
    assert result["compiler_codes.json"] == ["BCP001", "BCP033"]
    assert result["bicepconfig.json"]["analyzers"]["core"]["rules"] == {
        "new-rule": {"level": "off"},
        "sample-rule": {"level": "error", "maxAge": 365},
    }
    assert result["catalog_source.json"]["upstream_rules"]["sample-rule"] == {
        "level": "warning",
        "maxAge": 730,
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing-implementation",
        "extra-implementation",
        "duplicate-code",
        "missing-compiler",
        "unknown-level",
    ],
)
def test_source_drift_fails_closed(change):
    data = source()
    if change == "missing-implementation":
        del data[catalog.RULES + "New.cs"]
    elif change == "extra-implementation":
        data[catalog.RULES + "Other.cs"] = 'const string Code = "other-rule";'
    elif change == "duplicate-code":
        data[catalog.RULES + "Other.cs"] = data[catalog.RULES + "New.cs"]
    elif change == "missing-compiler":
        data[catalog.COMPILER] = '// "BCP033"'
    else:
        data[catalog.SCHEMA] = data[catalog.SCHEMA].replace("level-warning", "level-unknown")
    with pytest.raises(ValueError):
        catalog.extract(data)


def test_proposal_names_additions_removals_and_changed_defaults():
    after = catalog.generate(source(), "v1.2.3", "a" * 40, {})
    before = copy.deepcopy(after)
    before["compiler_codes.json"] = ["BCP002"]
    before["bicepconfig.json"]["analyzers"]["core"]["rules"].pop("new-rule")
    before["catalog_source.json"]["upstream_rules"]["sample-rule"]["level"] = "off"
    body = catalog.proposal(before, after)
    assert "Compiler codes added: `BCP001`, `BCP033`" in body
    assert "Compiler codes removed: `BCP002`" in body
    assert "Linter rules added: `new-rule`" in body
    assert "Upstream settings changed: `sample-rule`" in body
    assert "Live compiler execution is not verified" in body


def test_offline_regeneration_and_check_never_change_policy(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    for name, text in source().items():
        path = upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    output = tmp_path / "package"
    arguments = [
        "update",
        "--source",
        str(upstream),
        "--release",
        "v1.2.3",
        "--commit",
        "a" * 40,
        "--output",
        str(output),
    ]
    monkeypatch.setattr(catalog, "fetch", lambda *a: pytest.fail("offline mode used the network"))
    monkeypatch.setattr(sys, "argv", arguments)
    assert catalog.main() == 0
    snapshots = {name: (output / name).read_bytes() for name in catalog.FILES}
    monkeypatch.setattr(sys, "argv", [*arguments, "--check"])
    assert catalog.main() == 0
    newer = ["v1.2.4" if value == "v1.2.3" else value for value in arguments]
    monkeypatch.setattr(sys, "argv", [*newer, "--check"])
    assert catalog.main() == 0
    (upstream / catalog.COMPILER).write_text('CoreError("BCP002", "new");')
    assert catalog.main() == 1
    assert snapshots == {name: (output / name).read_bytes() for name in catalog.FILES}


def test_archive_reader_rejects_links_and_never_extracts_files(monkeypatch):
    import io
    import tarfile

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        member = tarfile.TarInfo("root/" + catalog.COMPILER)
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        output.addfile(member)
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda url: (
            json.dumps({"sha": "a" * 40}).encode() if "/commits/" in url else archive.getvalue()
        ),
    )
    with pytest.raises(ValueError, match="source archive"):
        catalog.download("v1.2.3")


@pytest.mark.workflow
def test_workflow_only_proposes_catalog_files_and_runs_regressions():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/bicep-catalog.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    job = workflow["jobs"]["propose"]
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    steps = job["steps"]
    proposal = next(step for step in steps if "create-pull-request@" in step.get("uses", ""))
    assert proposal["with"]["branch"] == "antsok-bicep-catalog-update"
    assert {Path(path).name for path in proposal["with"]["add-paths"].splitlines()} == set(
        catalog.FILES
    )
    validation = next(step["run"] for step in steps if step.get("name", "").startswith("Validate"))
    assert "test_update_bicep_catalog.py" in validation
    assert "test_bicep_catalog.py" in validation
    assert "test_bicep_workload.py" in validation
    assert "merge" not in proposal["with"]
