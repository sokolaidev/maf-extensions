"""Record pinned MXC host readiness and optional disposable-runner preparation."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
from pathlib import Path

from .commands import capture
from .evidence import Refused, file_hash, require
from .install import REPO, environment
from .mxc_smoke import identities

WINDOWS = os.name == "nt"
PREPARER = "eae22ec72df6b350aebbaa8d18e6790d328d11939bdd7a61ef0a38a62d4dcbd6"


def require_disposable_runner(root: Path, installation: Path) -> None:
    """Restrict preparation to the opt-in GitHub-hosted Windows workflow."""
    require(
        WINDOWS
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("RUNNER_OS") == "Windows",
        "disposable_windows_runner_required",
    )
    temporary = os.environ.get("RUNNER_TEMP")
    require(temporary, "runner_temp_required")
    assert temporary is not None
    parent = Path(temporary).resolve()
    require(
        root != parent
        and root.is_relative_to(parent)
        and installation != parent
        and installation.is_relative_to(parent),
        "runner_owned_paths_required",
    )


def observe(installation: Path, root: Path, *, prepare: bool) -> bool:
    """Record readiness separately from workload success; preparation never qualifies execution."""
    require(WINDOWS, "windows_required")
    require(
        not root.exists()
        and not root.is_relative_to(REPO)
        and not root.is_relative_to(installation)
        and not installation.is_relative_to(root),
        "fresh_external_output_required",
    )
    if prepare:
        require_disposable_runner(root, installation)
    root.mkdir()
    for name in ("temp", "appdata", "local"):
        (root / name).mkdir()
    lines = [
        "# Official MXC host diagnostics",
        "",
        f"Host: Windows {platform.version()}, {platform.machine()}; Python {platform.python_version()}.",
        f"Runner image: {os.environ.get('ImageOS', 'unrecorded')} / {os.environ.get('ImageVersion', 'unrecorded')}.",
        f"Source commit: {os.environ.get('GITHUB_SHA', 'unrecorded')}.",
        f"Preparation requested: `{prepare}`.",
        "",
        "Readiness and preparation are prerequisites, not native execution or confinement proof.",
    ]
    passed = False
    try:
        before = identities(installation)
        binary = (
            installation
            / "node_modules/@openclaw/mxc-sandbox/node_modules/@microsoft/mxc-sdk/bin/x64"
        )
        preparer = binary / "wxc-host-prep.exe"
        require(file_hash(preparer) == PREPARER, "host_preparer_identity")
        lines.append(f"Host preparer SHA-256: `{PREPARER}`.")
        executable = str(binary / "wxc-exec.exe")
        drive = Path(os.environ["SYSTEMROOT"]).anchor
        env = environment(root)
        env["SystemDrive"] = drive.removesuffix("\\")

        def record(label: str, args: list[str], *, fingerprint: bool = False) -> None:
            diagnostic_log = root / "setup.log"
            offset = diagnostic_log.stat().st_size if diagnostic_log.exists() else 0
            result = capture(args, root, env, 60, diagnostics=True, stdout_limit=16384)
            error_output = b""
            if diagnostic_log.exists():
                with diagnostic_log.open("rb") as stream:
                    stream.seek(offset)
                    error_output = stream.read(16385)
            lines.extend(["", f"## {label}", "", f"Exit code: {result.returncode}."])
            if fingerprint:
                lines.append(
                    f"Observed ACL output SHA-256: `{hashlib.sha256(result.stdout).hexdigest()}`."
                )
            outputs = [("stderr", error_output[:16384])]
            if not fingerprint:
                outputs.insert(0, ("stdout", result.stdout))
            for channel, raw in outputs:
                if not raw:
                    continue
                lines.append(f"{channel}:")
                text = raw.decode("utf-8", errors="replace")
                for value, replacement in (
                    (str(root), "<host-diagnostic>"),
                    (str(installation), "<installation>"),
                    (os.environ.get("USERPROFILE", ""), "<profile>"),
                ):
                    if value:
                        text = text.replace(value.replace("\\", "\\\\"), replacement).replace(
                            value, replacement
                        )
                lines.extend(["```text", text.replace("```", "---"), "```"])
            if len(error_output) > 16384:
                lines.append("Stderr exceeded 16 KiB; extraction is incomplete.")
            require(result.returncode == 0, "host_diagnostic_command")

        node = shutil.which("node")
        require(node is not None, "node_missing")
        assert node is not None
        record("Node version", [node, "--version"])
        acl = str(Path(os.environ["SYSTEMROOT"]) / "System32/icacls.exe")
        record("Initial system-drive ACL fingerprint", [acl, drive], fingerprint=True)
        record("Initial native readiness", [executable, "--probe"])
        if prepare:
            record("Disposable host preparation", [str(preparer), "prepare-system-drive"])
            record("Prepared system-drive ACL fingerprint", [acl, drive], fingerprint=True)
            record("Prepared native readiness", [executable, "--probe"])
        require(
            identities(installation) == before and file_hash(preparer) == PREPARER,
            "candidate_changed",
        )
        passed = True
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        lines.extend(["", f"Host diagnostics incomplete: {reason}."])
    finally:
        lines.extend(
            [
                "",
                f"Host diagnostic steps completed: `{passed}`. Native smoke is evaluated separately.",
            ]
        )
        (root / "host.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed


def main() -> int:
    """Run host diagnostics with explicit opt-in for disposable-runner preparation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    require(
        args.installation.is_absolute() and args.output.is_absolute(), "absolute_paths_required"
    )
    return (
        0
        if observe(args.installation.resolve(), args.output.resolve(), prepare=args.prepare)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
