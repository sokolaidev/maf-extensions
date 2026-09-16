# Supply a trusted builtin-profile base; deploy the result by immutable image ID/digest.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE}
RUN python3 -I -c 'from pathlib import Path; assert not any(Path("/opt/maf-terraform/mirror").rglob("*")), "base mirror must be empty"'
COPY mirror/ /opt/maf-terraform/mirror/
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
            found[path.relative_to(root).as_posix()] = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
    return found


expected = {}
for provider in receipt["providers"]:
    name = provider["source"].split("/")[-1]
    path = f'{provider["source"]}/terraform-provider-{name}_{provider["version"]}_{provider["platform"]}.zip'
    assert path not in expected
    expected[path] = provider["sha256"]
assert inventory(p / "mirror") == expected, "mirror must contain exactly the verified artifacts"
expected = {
    f'{module["name"]}/{name}': digest
    for module in receipt.get("registry_modules", [])
    for name, digest in module["files"].items()
}
assert inventory(p / "registry") == expected, "registry modules must contain exactly the verified files"
PY
# Initialize every baked registry package offline through the launcher before publishing.
RUN python3 -I - <<'PY'
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path
receipt = json.loads(Path("/opt/maf-terraform/dependencies.json").read_text())
packages = receipt.get("registry_modules", [])
if packages:
    call = Path(tempfile.mkdtemp())
    (call / "project").mkdir()
    (call / "project" / "main.tf").write_text(
        "".join(
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
    os.chdir("/")
    shutil.rmtree(call)
PY
