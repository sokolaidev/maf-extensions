# Supply a trusted builtin-profile base; deploy the result by immutable image ID/digest.
# The mirror leaves this build holding unpacked providers, which Terraform links into
# every call instead of copying; the ZIPs never reach a published layer.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE} AS unpack
COPY mirror/ /opt/maf-terraform/mirror/
COPY registry/ /opt/maf-terraform/registry/
COPY receipt.json /opt/maf-terraform/dependencies.json
RUN python3 -I - <<'PY'
import base64
import hashlib
import json
import zipfile
from pathlib import Path

root = Path("/opt/maf-terraform")
receipt = json.loads((root / "dependencies.json").read_text())
expected = {}
for provider in receipt["providers"]:
    name = provider["source"].split("/")[-1]
    path = f'{provider["source"]}/terraform-provider-{name}_{provider["version"]}_{provider["platform"]}.zip'
    assert path not in expected
    expected[path] = provider
found = {}
for path in (root / "mirror").rglob("*"):
    assert not path.is_symlink()
    if path.is_file():
        found[path.relative_to(root / "mirror").as_posix()] = hashlib.file_digest(
            path.open("rb"), "sha256"
        ).hexdigest()
assert found == {path: provider["sha256"] for path, provider in expected.items()}, (
    "mirror must contain exactly the verified artifacts"
)


def package_hash(paths):
    lines = "".join(f"{digest}  {path}\n" for path, digest in sorted(paths.items()))
    return "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


for path, provider in expected.items():
    source = root / "unpacked" / provider["source"] / provider["version"] / provider["platform"]
    with zipfile.ZipFile(root / "mirror" / path) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            assert entry.filename in provider["files"], entry.filename
            target = source / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(entry.filename)
            target.write_bytes(data)
            target.chmod(0o755)
            assert hashlib.sha256(data).hexdigest() == provider["files"][entry.filename], (
                entry.filename
            )
    unpacked = package_hash(
        {
            entry.relative_to(source).as_posix(): hashlib.sha256(entry.read_bytes()).hexdigest()
            for entry in source.rglob("*")
            if entry.is_file() and not entry.is_symlink()
        }
    )
    assert unpacked == provider["h1"], path
PY
FROM ${BASE_IMAGE}
RUN python3 -I -c 'from pathlib import Path; assert not any(Path("/opt/maf-terraform/mirror").rglob("*")), "base mirror must be empty"'
COPY --from=unpack /opt/maf-terraform/unpacked/ /opt/maf-terraform/mirror/
COPY registry/ /opt/maf-terraform/registry/
COPY receipt.json /opt/maf-terraform/dependencies.json
RUN python3 -I - <<'PY'
import hashlib
import json
from pathlib import Path

p = Path("/opt/maf-terraform")
receipt = json.loads((p / "dependencies.json").read_text())
metadata = json.loads((p / "engine.json").read_text())
assert metadata["engine"] == receipt["engine"]
metadata["profile"] = "prepared"
metadata["dependencies_manifest_sha256"] = receipt["manifest_sha256"]
metadata["dependencies_policy_sha256"] = receipt["policy_sha256"]
(p / "engine.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
written = json.loads((p / "engine.json").read_text())
assert written["engine"] == receipt["engine"]
assert written["profile"] == "prepared"
assert written["dependencies_manifest_sha256"] == receipt["manifest_sha256"]
assert written["dependencies_policy_sha256"] == receipt["policy_sha256"]


def inventory(root):
    found = {}
    for path in root.rglob("*"):
        assert not path.is_symlink()
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.file_digest(
                path.open("rb"), "sha256"
            ).hexdigest()
    return found


expected = {
    f'{module["name"]}/{name}': digest
    for module in receipt.get("registry_modules", [])
    for name, digest in module["files"].items()
}
assert inventory(p / "registry") == expected, "registry modules must contain exactly the verified files"
PY
# Initialize every provider and baked registry package offline through the launcher.
RUN python3 -I - <<'PY'
import importlib.util
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

root = Path("/opt/maf-terraform")
receipt = json.loads((root / "dependencies.json").read_text())
providers = receipt.get("providers", [])
packages = receipt.get("registry_modules", [])
requirements = "".join(
    f'    {"-".join(provider["source"].split("/")[-2:])} = {{ source = "{provider["source"]}", version = "{provider["version"]}" }}\n'
    for provider in providers
)
call = Path(tempfile.mkdtemp())
(call / "project").mkdir()
(call / "project" / "main.tf").write_text(
    (f"terraform {{\n  required_providers {{\n{requirements}  }}\n}}\n" if providers else "")
    + "".join(
        f'module "package_{index}" {{\n  source  = "{package["source"]}"\n  version = "{package["version"]}"\n}}\n'
        for index, package in enumerate(packages)
    )
)
spec = importlib.util.spec_from_file_location("runner", "/opt/maf-terraform/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
os.chdir(call)
result = runner.execute(receipt["engine"], ".", 300)
init = result["phases"].get("init", {})
assert result["error"] is None and init.get("exit_code") == 0, init.get("stderr", "")[-4000:]
for path in (root / "mirror").rglob("*"):
    if path.is_file():
        assert stat.S_IMODE(path.stat().st_mode) & 0o111, path
os.chdir("/")
shutil.rmtree(call)
PY
