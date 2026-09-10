"""Module-cache checks on Docker; opt in with MAF_SANDBOX_BICEP_E2E_IMAGE."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from maf_sandbox_bicep._tool import _BUILD_CMD, _BUILD_PARAMS_CMD, _LINT_CMD

_IMAGE = os.environ.get("MAF_SANDBOX_BICEP_E2E_IMAGE")
pytestmark = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker"),
    reason="needs Docker and MAF_SANDBOX_BICEP_E2E_IMAGE",
)


@pytest.mark.parametrize(
    ("template", "name"),
    [(_BUILD_CMD, "main.bicep"), (_BUILD_PARAMS_CMD, "main.bicepparam"), (_LINT_CMD, "main.bicep")],
)
def test_each_phase_restores_modules_under_its_call_path(template: str, name: str):
    assert _IMAGE is not None
    guest_call_path = "/maf-sandbox/work/cache-probe"
    command = template.format(path=f"{guest_call_path}/{name}")
    script = f"""set -eu
test ! -e /root/.bicep
test ! -e /tmp/.bicep
for round in 1 2; do
    mkdir -p {guest_call_path}
    cd {guest_call_path}
    test ! -e .bicep
    cat > main.bicep <<'BICEP'
module storage 'br/public:avm/res/storage/storage-account:0.31.0' = {{
  name: 'storage'
  params: {{
    name: 'teststorage'
  }}
}}
BICEP
    cat > main.bicepparam <<'PARAMS'
using './main.bicep'
PARAMS
    {command}
    test -d .bicep/br
    test -f .bicep/bicep.profile
    test ! -e /root/.bicep
    test ! -e /tmp/.bicep
    cd /
    rm -rf {guest_call_path}
done
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "sh", _IMAGE],
        input=script.encode(),
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout.decode().strip()
    decoder = json.JSONDecoder()
    for _ in range(2):
        sarif, end = decoder.raw_decode(output)
        assert all(
            result.get("ruleId") == "use-recent-module-versions"
            and result.get("level", "warning") == "warning"
            for run in sarif["runs"]
            for result in run.get("results", [])
        ), sarif
        output = output[end:].strip()
    assert not output
