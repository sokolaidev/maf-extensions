# Supply a trusted builtin-profile base; deploy the result by immutable image ID/digest.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE}
RUN python3 -I -c 'from pathlib import Path; assert not any(Path("/opt/maf-terraform/mirror").rglob("*")), "base mirror must be empty"'
COPY mirror/ /opt/maf-terraform/mirror/
COPY receipt.json /opt/maf-terraform/dependencies.json
RUN python3 -I - <<'PY'
import hashlib
import json
from pathlib import Path
p = Path("/opt/maf-terraform")
receipt = json.loads((p / "dependencies.json").read_text())
assert json.loads((p / "engine.json").read_text())["engine"] == receipt["engine"]
expected = {}
for provider in receipt["providers"]:
    name = provider["source"].split("/")[-1]
    path = f'{provider["source"]}/terraform-provider-{name}_{provider["version"]}_{provider["platform"]}.zip'
    assert path not in expected
    expected[path] = provider["sha256"]
actual = {}
for path in (p / "mirror").rglob("*"):
    assert not path.is_symlink()
    if path.is_file():
        actual[path.relative_to(p / "mirror").as_posix()] = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
assert actual == expected, "mirror must contain exactly the verified artifacts"
PY
