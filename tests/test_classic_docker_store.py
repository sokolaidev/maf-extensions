"""Keep configuration-ID image jobs on the effective classic Docker image store."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from classic_docker_store import configure, verify  # noqa: E402

pytestmark = pytest.mark.workflow
ACTION = "./.github/actions/setup-classic-docker"


def test_configuration_preserves_other_daemon_and_feature_settings():
    original = {
        "log-driver": "local",
        "features": {"buildkit": True, "containerd-snapshotter": True},
    }
    result = configure(original)
    assert result == {
        "log-driver": "local",
        "features": {"buildkit": True, "containerd-snapshotter": False},
    }
    assert original["features"]["containerd-snapshotter"] is True
    assert configure(result) == result
    assert configure({}) == {"features": {"containerd-snapshotter": False}}


@pytest.mark.parametrize("value", [None, [], {"features": None}, {"features": True}])
def test_invalid_daemon_configuration_is_refused(value):
    with pytest.raises(ValueError, match="must be an object"):
        configure(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        None,
        {"OSType": "windows", "Driver": "overlay2"},
        {"OSType": "linux", "Driver": "overlayfs"},
        {
            "OSType": "linux",
            "Driver": "overlay2",
            "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
        },
    ],
)
def test_unqualified_effective_store_is_refused(value):
    with pytest.raises(ValueError, match="classic Linux overlay2"):
        verify(value)


def test_classic_store_is_accepted():
    verify(
        {"OSType": "linux", "Driver": "overlay2", "DriverStatus": [["Backing Filesystem", "extfs"]]}
    )


@pytest.mark.parametrize(
    ("operation", "payload", "succeeds"),
    [
        ("configure", "{}", True),
        ("configure", "{broken", False),
        ("verify", '{"OSType":"linux","Driver":"overlay2"}', True),
        ("verify", '{"OSType":"linux","Driver":"overlayfs"}', False),
        ("unknown", "{}", False),
    ],
)
def test_json_pipeline_cli(operation, payload, succeeds):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/classic_docker_store.py"), operation],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is succeeds
    if operation == "configure" and succeeds:
        assert json.loads(result.stdout) == {"features": {"containerd-snapshotter": False}}


@pytest.mark.parametrize(
    ("filename", "job_name", "command"),
    [
        ("container-image-monitor.yml", "scan", "scripts/container_release_monitor.py scan"),
        ("container-image-release.yml", "prepare", "scripts/prepare_container_release_dispatch.py"),
        ("container-image-release.yml", "promote", "scripts/container_release_publish.py promote"),
        ("container-image-release.yml", "qualify", "scripts/container_release_publish.py qualify"),
        ("image-security.yml", "scan", "scripts/build_scan_image.sh"),
    ],
)
def test_image_jobs_configure_the_store_before_using_image_ids(filename, job_name, command):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text("utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    setup = [i for i, step in enumerate(steps) if step.get("uses") == ACTION]
    assert len(setup) == 1
    assert "if" not in steps[setup[0]]
    assert setup[0] < next(i for i, step in enumerate(steps) if command in step.get("run", ""))
    for i, step in enumerate(steps):
        if step.get("uses") == "./.github/actions/setup-sbx":
            assert i < setup[0]
