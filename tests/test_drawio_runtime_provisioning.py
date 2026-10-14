"""Provisioning boundaries and retained notices without privileged installation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux Bash paths")
SCRIPT = Path(__file__).resolve().parents[1] / "images/drawio-export/build-runtime.sh"


@pytest.fixture
def provisioning_env(tmp_path: Path) -> dict[str, str]:
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "id": "#!/bin/bash\nprintf '0\\n'\n",
        "debootstrap": '#!/bin/bash\nprintf "%s\\n" "$@" > "$PROVISION_MARKER"\nexit 97\n',
    }.items():
        script = commands / name
        script.write_text(body)
        script.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{commands}:/usr/bin:/bin",
        "PROVISION_MARKER": str(tmp_path / "marker"),
    }


@pytest.mark.parametrize("destination", ["runtime", "./runtime", "../runtime", ".", ""])
def test_relative_destination_is_refused(
    tmp_path: Path, provisioning_env: dict[str, str], destination: str
) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), destination],
        cwd=tmp_path,
        env=provisioning_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Destination must be absolute" in result.stderr
    assert not Path(provisioning_env["PROVISION_MARKER"]).exists()


def test_absolute_destination_reaches_provisioner(
    tmp_path: Path, provisioning_env: dict[str, str]
) -> None:
    target = tmp_path / "runtime space"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        cwd=tmp_path,
        env=provisioning_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 97
    assert Path(provisioning_env["PROVISION_MARKER"]).read_text().splitlines()[3] == str(target)
    assert not target.exists()


def test_native_installer_receives_the_checked_in_font_notice(
    tmp_path: Path, provisioning_env: dict[str, str]
) -> None:
    commands = tmp_path / "bin"
    for name, body in {
        "debootstrap": '#!/bin/bash\nmkdir -p -- "$4"\n',
        "sha256sum": "#!/bin/bash\ncat >/dev/null\n",
        "chroot": "\n".join(
            [
                "#!/bin/bash",
                "set -eu",
                'if test "$2" = python3; then',
                '  notice="$1/usr/share/licenses/drawio/DEJAVU-LICENSE"',
                '  cmp -- "$EXPECTED_NOTICE" "$notice"',
                '  test "$(stat -c %a -- "$notice")" = 644',
                '  printf "%s\\n" "$3" >> "$PROVISION_MARKER"',
                "fi",
                "",
            ]
        ),
    }.items():
        script = commands / name
        script.write_text(body)
        script.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "runtime space")],
        cwd=tmp_path,
        env={**provisioning_env, "EXPECTED_NOTICE": str(SCRIPT.with_name("DEJAVU-LICENSE"))},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(provisioning_env["PROVISION_MARKER"]).read_text().splitlines() == [
        "/opt/maf-drawio/install.py",
        "/opt/maf-drawio/check-assets.py",
    ]
