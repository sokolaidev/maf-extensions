"""Build the reviewed Bicep source with explicit dependency overrides and a receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from container_image_inputs import stage_image

RECEIPT = "/usr/share/maf-bicep/build.json"
LEGACY_PACKAGES = {"System.Security.Cryptography.Xml", "System.Security.Cryptography.Pkcs"}
PACKAGES = LEGACY_PACKAGES | {"MessagePack"}


def receipt_command() -> str:
    """Emit a JSON member for source builds while preserving legacy upstream images."""
    return (
        "printf ',\"buildReceipt\":'; "
        f"if test -f {RECEIPT}; then cat {RECEIPT}; else printf null; fi"
    )


def verify_receipt(receipt: Any, compiler: str, context: Path) -> dict[str, Any]:
    """Bind a source-built binary to the reviewed policy and builder, or enforce the upstream pin."""
    from bicep_image_pin import read_pin

    version, upstream = read_pin(context / "Dockerfile")
    if receipt is None:
        if compiler != upstream:
            raise ValueError("Bicep compiler differs from the upstream pin")
        return {"route": "upstream", "compilerSha256": compiler}
    data = policy(context / "source.json")
    expected = {
        "route": "source",
        **data,
        "policySha256": sha256(context / "source.json"),
        "builderSha256": sha256(Path(__file__)),
        "compilerSha256": compiler,
    }
    if (
        version != "v" + data["version"]
        or not re.fullmatch(r"[0-9a-f]{64}", compiler)
        or receipt != expected
    ):
        raise ValueError("Bicep source build receipt differs from the reviewed inputs or binary")
    return expected


def sha256(path: Path) -> str:
    """Hash the retained bytes of a build input or output."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_inventory(sbom: dict[str, Any], runtime: dict[str, Any]) -> None:
    """Require the source build's patched packages in the scanned application inventory."""
    build = runtime.get("bicepAvm", runtime.get("bicep", {})).get("build", {})
    if build.get("route") != "source":
        return
    packages = build.get("packages", {})
    # Published receipts retain the package policy used to build their immutable images.
    if set(packages) not in (LEGACY_PACKAGES, PACKAGES):
        raise ValueError("Bicep source dependency evidence is incomplete")
    for name, version in packages.items():
        found = {
            item.get("version")
            for item in sbom["artifacts"]
            if item.get("name") == name and item.get("type") == "dotnet"
        }
        if found != {version}:
            raise ValueError(f"Bicep inventory omits or contradicts the patched package: {name}")


def policy(path: Path) -> dict[str, Any]:
    """Reject floating source, SDK and dependency inputs before executing build tools."""
    data = json.loads(path.read_bytes())
    if set(data) != {"version", "sourceCommit", "runtimeVersion"}:
        raise ValueError("Invalid pinned Bicep source policy")
    sdk = stage_image(path.with_name("source.Dockerfile"), "build")
    project = ET.parse(path.with_name("dependencies.csproj"))  # nosec B314 # Reviewed build manifest.
    references = project.findall("./ItemGroup/PackageReference")
    packages = {item.attrib["Include"]: item.attrib["Version"] for item in references}
    if len(packages) != len(references):
        raise ValueError("Duplicate Bicep package pin")
    data.update(sdkImage=sdk, sdkVersion=sdk.split(":")[-2].split("@")[0], packages=packages)
    if (
        set(data)
        != {"version", "sourceCommit", "sdkImage", "sdkVersion", "runtimeVersion", "packages"}
        or not re.fullmatch(r"[0-9a-f]{40}", data["sourceCommit"])
        or not re.fullmatch(
            r"mcr\.microsoft\.com/dotnet/sdk:"
            + re.escape(data["sdkVersion"])
            + r"@sha256:[0-9a-f]{64}",
            data["sdkImage"],
        )
        or set(data["packages"]) != PACKAGES
        or any(
            not re.fullmatch(r"\d+\.\d+\.\d+", value)
            for value in (
                data["version"],
                data["sdkVersion"],
                data["runtimeVersion"],
                *data["packages"].values(),
            )
        )
    ):
        raise ValueError("Invalid pinned Bicep source policy")
    return data


def patch_packages(path: Path, packages: dict[str, str]) -> None:
    """Use central transitive pins so the published dependency graph includes the fixes."""
    tree = ET.parse(path)
    root = tree.getroot()
    if root.findtext("./PropertyGroup/CentralPackageTransitivePinningEnabled") != "true":
        raise ValueError("Bicep source must enable central transitive pinning")
    group = ET.SubElement(root, "ItemGroup")
    for name, version in packages.items():
        matches = root.findall(f"./ItemGroup/PackageVersion[@Include='{name}']")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous package pin: {name}")
        element = matches[0] if matches else ET.SubElement(group, "PackageVersion", Include=name)
        element.set("Version", version)
    tree.write(path, encoding="utf-8", xml_declaration=False)


def resolved_packages(path: Path, packages: dict[str, str]) -> dict[str, str]:
    """Check the restored graph rather than assuming an override took effect."""
    libraries = json.loads(path.read_bytes())["libraries"]
    resolved = {}
    for name, version in packages.items():
        matches = [key.split("/", 1)[1] for key in libraries if key.split("/", 1)[0] == name]
        if matches != [version]:
            raise ValueError(f"Published Bicep dependency differs from policy: {name}")
        resolved[name] = version
    return resolved


def run(*command: str, cwd: Path, capture: bool = True) -> str:
    """Preserve compiler diagnostics on both successful and failed builds."""
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=1800,
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    result.check_returncode()
    return (result.stdout or "").strip()


def build(policy_path: Path, output: Path) -> None:
    """Publish a self-contained compiler and record its resolved security dependencies."""
    data = policy(policy_path)
    run("dotnet", "restore", "dependencies.csproj", cwd=policy_path.parent, capture=False)
    resolved_packages(policy_path.parent / "obj/project.assets.json", data["packages"])
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="bicep-source-") as temporary:
        source = Path(temporary)
        run("git", "init", ".", cwd=source)
        run(
            "git",
            "fetch",
            "https://github.com/Azure/bicep.git",
            data["sourceCommit"],
            cwd=source,
        )
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=source)
        if run("git", "rev-parse", "HEAD", cwd=source) != data["sourceCommit"]:
            raise ValueError("Bicep source commit mismatch")
        if run("dotnet", "--version", cwd=source) != data["sdkVersion"]:
            raise ValueError("Bicep SDK version mismatch")
        patch_packages(source / "src/Directory.Packages.props", data["packages"])
        run(
            "dotnet",
            "publish",
            "src/Bicep.Cli/Bicep.Cli.csproj",
            "--configuration",
            "Release",
            "--runtime",
            "linux-x64",
            "--self-contained",
            "true",
            "--output",
            str(output),
            f"-p:RuntimeFrameworkVersion={data['runtimeVersion']}",
            f"-p:Version={data['version']}",
            f"-p:InformationalVersion={data['version']}+maf.{data['sourceCommit'][:12]}",
            cwd=source,
            capture=False,
        )
        resolved = resolved_packages(
            source / "src/Bicep.Cli/obj/project.assets.json", data["packages"]
        )
        notices = list((source / "src/Bicep.Cli/obj").rglob("ThirdPartyNotices.txt"))
        if len(notices) != 1:
            raise ValueError("Expected one generated Bicep CLI third-party notice")
        shutil.copyfile(notices[0], output / "NOTICE")
        receipt = {
            "route": "source",
            **data,
            "packages": resolved,
            "policySha256": sha256(policy_path),
            "builderSha256": sha256(Path(__file__)),
            "compilerSha256": sha256(output / "bicep"),
        }
        (output / "build.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def main() -> None:
    """Build inside the SDK stage using only the committed source policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.policy, args.output)


if __name__ == "__main__":
    main()
