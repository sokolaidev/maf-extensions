"""Exercise real runtime provenance verification and retain registry-independent evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

VERIFIER = Path(__file__).with_name("verify_hyperlight_aks_image.py")


def check_result(
    result: subprocess.CompletedProcess[str],
    output: Path,
    expected_error: str | tuple[str, ...] | None,
) -> None:
    """Accept a refusal only for the expected reason and with no stale success record."""
    if expected_error is None:
        if result.returncode or not output.is_file():
            raise ValueError("signed runtime verification failed")
        record = json.loads(output.read_text("utf-8"))
        if record.get("signed_provenance_verified") is not True:
            raise ValueError("signed runtime verification did not retain success")
    elif (
        result.returncode == 0
        or output.exists()
        or not any(
            error.casefold() in result.stderr.casefold()
            for error in ((expected_error,) if isinstance(expected_error, str) else expected_error)
        )
    ):
        raise ValueError("refusal did not fail for the expected reason or left stale evidence")


def exercise(
    image: str,
    unsigned_image: str,
    signer_identity: str,
    source_revision: str,
    source_ref: str,
    build_inputs_sha256: str,
    output: Path,
) -> None:
    """Run the real CLI in separate processes; publish evidence only after every check passes."""
    if image.rsplit("@", 1)[-1] == unsigned_image.rsplit("@", 1)[-1]:
        raise ValueError("unsigned candidate must have a different digest")
    output.mkdir(parents=True, exist_ok=True)
    evidence = output / "verification.json"
    evidence.unlink(missing_ok=True)
    policy = {
        "--image": image,
        "--signer-identity": signer_identity,
        "--source-revision": source_revision,
        "--source-ref": source_ref,
        "--build-inputs-sha256": build_inputs_sha256,
    }
    cases = [
        ("signed-runtime", {}, None),
        (
            "wrong-signer",
            {
                "--signer-identity": signer_identity.replace(
                    "/.github/workflows/", "/.github/workflows/wrong-"
                )
            },
            'Error: verifying with issuer "sigstore.dev"',
        ),
        (
            "wrong-source-revision",
            {
                "--source-revision": ("0" if source_revision[0] != "0" else "1")
                + source_revision[1:]
            },
            "expected SourceRepositoryDigest to be",
        ),
        (
            "wrong-source-ref",
            {"--source-ref": source_ref + "-wrong"},
            "expected SourceRepositoryRef to be",
        ),
        (
            "wrong-build-inputs",
            {
                "--build-inputs-sha256": ("0" if build_inputs_sha256[0] != "0" else "1")
                + build_inputs_sha256[1:]
            },
            "image build inputs do not match",
        ),
        (
            "unsigned-modified-image",
            {"--image": unsigned_image},
            (
                "no attestations",
                "HTTP 404: Not Found (https://api.github.com/repos/sokolaidev/maf-extensions/attestations/",
            ),
        ),
        ("signed-runtime-after-refusals", {}, None),
    ]
    results = []
    accepted = {}
    with tempfile.TemporaryDirectory() as temporary:
        record_path = Path(temporary) / "verification.json"
        for name, changes, expected in cases:
            if expected is None:
                record_path.unlink(missing_ok=True)
            else:
                record_path.write_text(json.dumps(accepted), encoding="utf-8")
            arguments = [sys.executable, str(VERIFIER)]
            for key, value in (policy | changes).items():
                arguments.extend([key, value])
            arguments.extend(["--output", str(record_path)])
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=240)
            try:
                check_result(result, record_path, expected)
            except ValueError:
                # CLI diagnostics can contain the registry host; mask it before logging.
                registry = image.split("/", 1)[0]
                print(
                    (result.stdout + result.stderr).replace(registry, "registry.example"),
                    file=sys.stderr,
                )
                raise
            if expected is None:
                accepted = json.loads(record_path.read_text("utf-8"))
                accepted.pop("image")
            results.append(
                {"case": name, "passed": True, "stale_record_removed": expected is not None}
            )
            print(f"PASS {name}")
    accepted["integration_cases"] = results
    accepted["unsigned_registry_digest"] = unsigned_image.rsplit("@", 1)[1]
    serialized = json.dumps(accepted, indent=2, sort_keys=True)
    if image.split("/", 1)[0] in serialized:
        raise ValueError("public evidence contains a private registry identifier")
    evidence.write_text(serialized + "\n", encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("### Signed runtime integration\n\n")
            stream.write("\n".join(f"- PASS {case['case']}" for case in results) + "\n")


def main() -> None:
    """Require explicit candidate digests and policy from the workflow's clean build."""
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "image",
        "unsigned-image",
        "signer-identity",
        "source-revision",
        "source-ref",
        "build-inputs-sha256",
    ):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    exercise(
        args.image,
        args.unsigned_image,
        args.signer_identity,
        args.source_revision,
        args.source_ref,
        args.build_inputs_sha256,
        args.output,
    )


if __name__ == "__main__":
    main()
