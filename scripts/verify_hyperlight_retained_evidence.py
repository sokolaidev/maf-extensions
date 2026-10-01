"""Verify one retained candidate's evidence using an independently supplied expected policy."""

import argparse
import json
import tarfile
import tempfile
from pathlib import Path

from hyperlight_evidence import extract_archive, read, sha, validate_candidate, verify_bundle

REQUIRED = {
    "signed-manifest.json",
    "metadata-layer.tar.gz",
    "source.json",
    "build-inputs.json",
    "attestation-bundles.jsonl",
    "trusted-root.jsonl",
    "promotion.json",
    "acceptance.json",
    "node-pull.json",
    "retention-policy.json",
    "storage-access.json",
    "storage-account.json",
    "retention-hold.json",
}


def _verify_directory(root: Path, expected: dict) -> dict:
    """Internal only: root must have passed the independently approved archive hash check."""
    inventory = read(root / "SHA256SUMS.json")
    if not REQUIRED <= set(inventory):
        raise ValueError("required evidence missing from inventory")
    for name, digest in inventory.items():
        path = root / name
        if Path(name).name != name or not path.is_file() or sha(path) != digest:
            raise ValueError("missing or altered evidence: " + name)
    if sha(root / "trusted-root.jsonl") != expected["trusted_root_sha256"]:
        raise ValueError("trust root differs from operator-approved snapshot")
    candidate = expected["candidate"]
    validate_candidate(candidate)
    image_digest = candidate["image"].split("@sha256:")[1]
    if sha(root / "signed-manifest.json") != image_digest:
        raise ValueError("signed manifest differs from expected image digest")
    manifest = read(root / "signed-manifest.json")
    if manifest["layers"][-1]["digest"] != "sha256:" + sha(root / "metadata-layer.tar.gz"):
        raise ValueError("metadata layer differs from signed manifest")
    with tarfile.open(root / "metadata-layer.tar.gz", "r:gz") as layer:
        for name in ("source.json", "build-inputs.json"):
            matches = [m for m in layer.getmembers() if m.name.lstrip("./") == "opt/" + name]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 1048576:
                raise ValueError("metadata layer lacks a unique bounded regular file")
            stream = layer.extractfile(matches[0])
            if stream is None or stream.read() != (root / name).read_bytes():
                raise ValueError("metadata differs from authenticated layer: " + name)
    if sha(root / "build-inputs.json") != candidate["build_inputs_sha256"]:
        raise ValueError("build input hash differs from expected policy")
    if read(root / "build-inputs.json")["source.json"] != sha(root / "source.json"):
        raise ValueError("source record is not bound by build inputs")
    source = read(root / "source.json")
    if (
        source["revision"] != candidate["source_revision"]
        or source["dirty"] is not False
        or source["repository"] != "https://github.com/sokolaidev/maf-extensions"
    ):
        raise ValueError("source record differs from approved clean source")
    verify_bundle(root, candidate, expected["trusted_root_sha256"])
    verifications = read(root / "promotion.json")["verifications"]
    if not isinstance(verifications, list) or any(
        not isinstance(record, dict) for record in verifications
    ):
        raise ValueError("promotion verifications must be a list of records")
    matches = [record for record in verifications if record.get("image") == candidate["image"]]
    if len(matches) != 1:
        raise ValueError("promotion requires exactly one verification for the candidate image")
    promotion = matches[0]
    if promotion["registry_digest"] != "sha256:" + image_digest:
        raise ValueError("promotion image association differs")
    for key in ("source_revision", "source_ref", "build_inputs_sha256", "signer_identity"):
        if promotion["policy"][key] != candidate[key]:
            raise ValueError("promotion policy association differs: " + key)
    if read(root / "acceptance.json")["candidate"] != candidate:
        raise ValueError("AKS admission candidate association differs")
    node = read(root / "node-pull.json")
    if (
        node["image"] != candidate["image"]
        or node["smoke"]["source"] != source
        or node["smoke"]["build_inputs_sha256"] != candidate["build_inputs_sha256"]
    ):
        raise ValueError("AKS pull candidate association differs")
    return {
        "signed_provenance_verified": True,
        "image_digest": "sha256:" + image_digest,
        "source_revision": source["revision"],
        "build_inputs_sha256": candidate["build_inputs_sha256"],
        "verified_files": len(inventory),
        "metadata_bound_to_signed_manifest": True,
        "host_reports_are_unsigned": True,
    }


def verify(archive: Path, receipt: Path) -> dict:
    """Check an archive against an independently supplied operator-approved receipt."""
    expected = read(receipt)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        extract_archive(archive, root, expected.get("archive_sha256"))
        return _verify_directory(root, expected)


def main() -> None:
    """Verify retained evidence without trusting an archive-provided receipt or executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive, args.receipt)))


if __name__ == "__main__":
    main()
