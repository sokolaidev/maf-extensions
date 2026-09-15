"""Prepared receipt paths must not read outside their module directory."""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "images/terraform-sandbox"))
import example as image_example  # noqa: E402


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "prepared"
    module = root / "modules/approved"
    module.mkdir(parents=True)
    (module / "main.tf").write_text('output "x" { value = 1 }')
    (tmp_path / "outside.tf").write_text('output "secret" { value = 2 }')
    return root


@pytest.mark.parametrize(
    "escape", ["module-parent", "module-absolute", "file-parent", "file-absolute"]
)
def test_receipt_escape_is_refused_before_read(prepared, monkeypatch, escape):
    outside = prepared.parent / "outside.tf"
    name, filename = "approved", "main.tf"
    if escape == "module-parent":
        name, filename = "../..", "outside.tf"
    elif escape == "module-absolute":
        name, filename = str(prepared.parent), "outside.tf"
    elif escape == "file-parent":
        filename = "../../../outside.tf"
    else:
        filename = str(outside)
    module = {"name": name, "files": {filename: hashlib.sha256(outside.read_bytes()).hexdigest()}}
    original_read = Path.read_bytes

    def guarded_read(path):
        assert path.resolve() != outside.resolve(), "receipt caused an outside read"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(ValueError, match="prepared module path"):
        image_example.prepared_module_files(prepared, module)


@pytest.mark.parametrize("link_kind", ["module", "file", "modules-root"])
def test_receipt_symlink_escape_is_refused_before_read(prepared, monkeypatch, link_kind):
    outside = prepared.parent / "outside.tf"
    name, filename = "approved", "main.tf"
    try:
        if link_kind == "module":
            (prepared / "modules/linked").symlink_to(prepared.parent, target_is_directory=True)
            name, filename = "linked", "outside.tf"
        elif link_kind == "file":
            (prepared / "modules/approved/linked.tf").symlink_to(outside)
            filename = "linked.tf"
        else:
            (prepared / "modules").rename(prepared / "saved-modules")
            (prepared / "modules").symlink_to(prepared / "saved-modules", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    module = {"name": name, "files": {filename: "0" * 64}}
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: pytest.fail("read through escaping symlink")
    )
    with pytest.raises(ValueError, match="prepared module path"):
        image_example.prepared_module_files(prepared, module)


def test_prepared_file_inventory_and_hash_are_preserved(prepared):
    data = (prepared / "modules/approved/main.tf").read_bytes()
    module = {"name": "approved", "files": {"main.tf": hashlib.sha256(data).hexdigest()}}
    assert image_example.prepared_module_files(prepared, module) == {
        "modules/approved/main.tf": data.decode()
    }
    module["files"]["main.tf"] = "0" * 64
    with pytest.raises(ValueError, match="content has changed"):
        image_example.prepared_module_files(prepared, module)
