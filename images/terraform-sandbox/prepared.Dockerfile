# Build from images/terraform-sandbox with `--build-context scripts=scripts` and a MANIFEST
# there. Supply a trusted builtin-profile base built from the same checkout; deploy the
# result by immutable image ID/digest. Only preparation reaches the network.
# The mirror leaves this build holding unpacked providers, which Terraform links into
# every call instead of copying; the ZIPs never reach a published layer.
ARG BASE_IMAGE=scratch
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS provider-toolchain
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache python3 git

FROM provider-toolchain AS provider-build
ARG MANIFEST
COPY --from=scripts terraform_dependencies.py terraform_manifest.py /src/scripts/
COPY runner.py build_providers.py /src/images/terraform-sandbox/
COPY ${MANIFEST} /src/manifest.json
# Mount the parent because go clean removes the module-cache directory itself.
RUN --mount=type=cache,target=/go/pkg --mount=type=cache,target=/root/.cache/go-build python3 /src/images/terraform-sandbox/build_providers.py --manifest /src/manifest.json --output /builds && go clean -cache -modcache

FROM scratch AS provider-packages
COPY --from=provider-build /builds/ /

FROM ${BASE_IMAGE} AS prepare
ARG MANIFEST
COPY --from=scripts terraform_dependencies.py /src/scripts/terraform_dependencies.py
COPY runner.py /src/images/terraform-sandbox/runner.py
COPY ${MANIFEST} /src/manifest.json
COPY --from=provider-build /builds/ /provider-builds/
RUN python3 -I /src/scripts/terraform_dependencies.py --manifest /src/manifest.json --output /prepared --provider-builds /provider-builds

# The preparation alone; export it with `--target prepared --output type=local,dest=<dir>`.
# Adding `--build-context prepared=<dir>` builds from a stored one without preparing again.
FROM scratch AS prepared
COPY --from=prepare /prepared/ /

FROM ${BASE_IMAGE} AS unpack
COPY --from=prepared /mirror/ /opt/maf-terraform/mirror/
COPY --from=prepared /registry/ /opt/maf-terraform/registry/
COPY --from=prepared /receipt.json /opt/maf-terraform/dependencies.json
RUN --network=none python3 -I - <<'PY'
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
RUN --network=none python3 -I -c 'from pathlib import Path; assert not any(Path("/opt/maf-terraform/mirror").rglob("*")), "base mirror must be empty"'
COPY --from=unpack /opt/maf-terraform/unpacked/ /opt/maf-terraform/mirror/
COPY --from=prepared /modules/ /opt/maf-terraform/modules/
COPY --from=prepared /registry/ /opt/maf-terraform/registry/
COPY --from=prepared /receipt.json /opt/maf-terraform/dependencies.json
RUN --network=none python3 -I - <<'PY'
import hashlib
import json
from pathlib import Path

p = Path("/opt/maf-terraform")
receipt = json.loads((p / "dependencies.json").read_text())
metadata = json.loads((p / "engine.json").read_text())
assert metadata["engine"] == receipt["engine"]
launcher = hashlib.sha256((p / "runner.py").read_text().encode()).hexdigest()
assert launcher == receipt["policy_contract"].get("reader_sha256"), (
    "the base launcher is not the reader preparation used; rebuild the base from this checkout"
)
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
# Initialize every provider version and every root package offline through the launcher.
# A root is a package no other package calls; the packages it calls initialize with it.
RUN --network=none python3 -I - <<'PY'
import importlib.util
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

root = Path("/opt/maf-terraform")
receipt = json.loads((root / "dependencies.json").read_text())
providers = receipt.get("providers", [])
packages = receipt.get("registry_modules", [])
ranks = {}
for provider in providers:
    ranks.setdefault(provider["source"], []).append(provider)
probes = []
for rank in range(max((len(items) for items in ranks.values()), default=0)):
    requirements = "".join(
        f'    {"-".join(items[rank]["source"].split("/")[-2:])} = {{ source = "{items[rank]["source"]}", version = "= {items[rank]["version"]}" }}\n'
        for items in ranks.values()
        if rank < len(items)
    )
    probes.append((f"providers rank {rank}", f"terraform {{\n  required_providers {{\n{requirements}  }}\n}}\n"))
called = {
    edge["registry"]
    for package in packages
    for edges in package["graph"].values()
    for edge in edges.values()
    if "registry" in edge
}
for package in packages:
    if package["name"] not in called:
        probes.append(
            (package["name"], f'module "root" {{\n  source  = "{package["source"]}"\n  version = "= {package["version"]}"\n}}\n')
        )


def probe_succeeded(label, result):
    phases = result["phases"]
    if result["error"] is not None or phases.get("init", {}).get("exit_code") != 0:
        return False
    if label.startswith("providers rank "):
        validation = phases.get("validate", {})
        return validation.get("exit_code") == 0 and json.loads(validation["stdout"]).get("valid") is True and result.get("schemasValid") is True
    return True


def probe(item):
    label, configuration = item
    spec = importlib.util.spec_from_file_location("runner", "/opt/maf-terraform/runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    call = Path(tempfile.mkdtemp())
    (call / "project").mkdir()
    (call / "project" / "main.tf").write_text(configuration)
    os.chdir(call)
    try:
        result = runner.execute(receipt["engine"], ".", 300)
        if label.startswith("providers rank ") and result["phases"].get("init", {}).get("exit_code") == 0:
            rank = int(label.rsplit(" ", 1)[1])
            expected = {source for source, items in ranks.items() if rank < len(items)}
            executable = "terraform" if receipt["engine"] == "terraform" else "tofu"
            with (call / "schemas.json").open("w") as output:
                schema = subprocess.run(
                    ["/usr/local/bin/" + executable, "providers", "schema", "-json"],
                    cwd=call / "project", env=runner.clean_environment(call / ".runner"),
                    stdout=output, stderr=subprocess.PIPE, timeout=300, check=False,
                )
            result["schemasValid"] = schema.returncode == 0 and set(json.loads((call / "schemas.json").read_text()).get("provider_schemas", {})) == expected
    finally:
        os.chdir("/")
        shutil.rmtree(call)
    init = result["phases"].get("init", {})
    ok = probe_succeeded(label, result)
    diagnostic = init.get("stderr", "") + result["phases"].get("validate", {}).get("stdout", "")
    return label, ok, diagnostic[-2000:]


provider_probes = [item for item in probes if item[0].startswith("providers rank ")]
module_probes = [item for item in probes if not item[0].startswith("providers rank ")]
failures = [(label, stderr) for label, ok, stderr in map(probe, provider_probes) if not ok]
with multiprocessing.get_context("fork").Pool(min(4, os.cpu_count() or 1)) as pool:
    failures.extend((label, stderr) for label, ok, stderr in pool.imap_unordered(probe, module_probes) if not ok)
for label, stderr in sorted(failures):
    print(f"offline init failed for {label}:\n{stderr}")
assert not failures, f"{len(failures)} of {len(probes)} offline probes failed"
print(f"{len(probes)} offline probes passed")
for path in (root / "mirror").rglob("*"):
    if path.is_file():
        assert stat.S_IMODE(path.stat().st_mode) & 0o111, path
PY
