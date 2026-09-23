"""Build an AKS runtime from workspace wheels and the locked dependency graph."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://github.com/sokolaidev/maf-extensions"
PROBE_COMMAND = [
    "python",
    "-I",
    "-u",
    "-m",
    "maf_sandbox_hyperlight._pod_supervisor",
    "python",
    "-I",
    "-u",
    "/opt/hyperlight-probe.py",
]


def source_record(*, require_clean: bool = False) -> dict[str, object]:
    """Record public source identity without exporting local paths or remote configuration."""
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if require_clean and status:
        raise ValueError("release image requires a clean source checkout")
    return {
        "repository": SOURCE_URL,
        "revision": revision,
        "dirty": bool(status),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    }


def prepare(destination: Path, *, require_clean: bool = False) -> None:
    """Include only built wheels, hashed dependencies and the public probe in the build context."""
    destination.mkdir(parents=True, exist_ok=True)
    allowed = {
        "wheels",
        "Dockerfile",
        ".dockerignore",
        "probe.py",
        "requirements.txt",
        "build-inputs.json",
        "bundle.json.gz",
        "source.json",
        "verify.py",
        "image-verification.json",
    }
    if any(path.name not in allowed for path in destination.iterdir()):
        raise ValueError("build output directory contains unrelated files")
    if destination.is_symlink() or any(path.is_symlink() for path in destination.rglob("*")):
        raise ValueError("build output must not contain symlinks")
    (destination / "image-verification.json").unlink(missing_ok=True)
    source = source_record(require_clean=require_clean)
    for package in ("maf-sandbox", "maf-sandbox-hyperlight", "maf-sandbox-codeact"):
        subprocess.run(
            [
                "uv",
                "build",
                "--package",
                package,
                "--wheel",
                "--out-dir",
                str(destination / "wheels"),
            ],
            cwd=ROOT,
            check=True,
        )
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--package",
            "maf-sandbox-hyperlight",
            "--package",
            "maf-sandbox-codeact",
            "--no-emit-workspace",
            "--no-header",
            "--output-file",
            str(destination / "requirements.txt"),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copyfile(ROOT / "images/hyperlight-sandbox/Dockerfile", destination / "Dockerfile")
    shutil.copyfile(ROOT / "images/hyperlight-sandbox/.dockerignore", destination / ".dockerignore")
    built = sorted(path for path in (destination / "wheels").iterdir() if path.name != ".gitignore")
    if len(built) != 3 or any(path.suffix != ".whl" for path in built):
        raise ValueError("build output contains stale or unexpected wheels; use a fresh directory")
    shutil.copyfile(ROOT / "samples/experimental/hyperlight-aks/probe.py", destination / "probe.py")
    shutil.copyfile(ROOT / "scripts/hyperlight_image_smoke.py", destination / "verify.py")
    (destination / "source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
    evidence = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in destination.rglob("*")
        if path.is_file()
        and path.relative_to(destination).as_posix()
        not in {"build-inputs.json", "bundle.json.gz", "wheels/.gitignore"}
    }
    (destination / "build-inputs.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    bundle = {
        "files": {
            name: (destination / name).read_text(encoding="utf-8")
            for name in ("requirements.txt", "probe.py")
        },
        "wheels": {
            path.name: base64.b64encode(path.read_bytes()).decode()
            for path in sorted((destination / "wheels").glob("*.whl"))
        },
    }
    encoded = gzip.compress(json.dumps(bundle, sort_keys=True).encode(), mtime=0)
    if len(encoded) > 700_000:
        raise ValueError("wheel bundle exceeds the ConfigMap transport bound; publish the image")
    (destination / "bundle.json.gz").write_bytes(encoded)
    print("bundle sha256:", hashlib.sha256(encoded).hexdigest())


def build_and_verify(destination: Path, tag: str) -> dict[str, object]:
    """Verify the immutable local build output and retain an unsigned evidence record."""
    record_path = destination / "image-verification.json"
    record_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="maf-hyperlight-iid-") as temporary:
        iid_path = Path(temporary) / "image-id"
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--iidfile",
                str(iid_path),
                "--tag",
                tag,
                str(destination),
            ],
            check=True,
        )
        image_id = iid_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Docker did not return an immutable image ID")
    details = json.loads(
        subprocess.run(
            ["docker", "image", "inspect", image_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    if (details["Os"], details["Architecture"], details["Config"]["User"]) != (
        "linux",
        "amd64",
        "65534:65534",
    ):
        raise ValueError("image platform or default user does not match the runtime contract")
    if details["Config"].get("Entrypoint") or details["Config"].get("Cmd") != PROBE_COMMAND:
        raise ValueError("image default command does not use the pod supervisor and probe")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--entrypoint",
            "python",
            image_id,
            "-I",
            "-B",
            "/opt/verify.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    smoke = json.loads(result.stdout)
    expected = hashlib.sha256((destination / "build-inputs.json").read_bytes()).hexdigest()
    if smoke["build_inputs_sha256"] != expected:
        raise ValueError("image build inputs do not match the prepared context")
    record = {
        "schema_version": 1,
        "local_image_id": image_id,
        "registry_digest": None,
        "signed_provenance_verified": False,
        "smoke": smoke,
    }
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def main() -> None:
    """Prepare a reviewable context; build locally only when an image tag is supplied."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tag", help="Build locally and smoke-check the immutable image ID")
    parser.add_argument("--require-clean", action="store_true", help="Reject uncommitted source")
    args = parser.parse_args()
    destination = args.output or Path(tempfile.mkdtemp(prefix="maf-hyperlight-image-"))
    prepare(destination, require_clean=args.require_clean)
    if args.tag:
        record = build_and_verify(destination, args.tag)
        print("verified local image:", record["local_image_id"])
    print(destination)


if __name__ == "__main__":
    main()
