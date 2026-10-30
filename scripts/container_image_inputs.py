"""Read immutable external image inputs from the Dockerfiles that build them."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PIN = re.compile(r"(?P<name>[^\s@]+):(?P<tag>[^\s:@]+)@sha256:[0-9a-f]{64}")


def stage_image(path: Path, stage: str) -> str:
    """Read one literal, digest-pinned named stage without accepting ARG overrides."""
    matches = re.findall(
        rf"^FROM\s+(\S+)\s+AS\s+{re.escape(stage)}\s*$",
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.IGNORECASE,
    )
    if len(matches) != 1 or PIN.fullmatch(matches[0]) is None:
        raise ValueError(f"Expected one pinned literal stage {stage}: {path}")
    return matches[0]


def inventory(path: Path) -> list[str]:
    """Distinguish external images from scratch, named stages and supplied parents."""
    stages: set[str] = set()
    arguments: dict[str, str] = {}
    images: list[str] = []
    heredoc = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = ""
            continue
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0].upper() in {"RUN", "COPY"}:
            marker = re.search(r"<<-?['\"]?([A-Za-z_][A-Za-z_0-9]*)", line)
            if marker:
                heredoc = marker[1]
        if fields[0].upper() == "COPY":
            for field in fields[1:]:
                if field.startswith("--from="):
                    source = field.removeprefix("--from=")
                    if source not in stages | {"scripts", "suite", "proxy"}:
                        if PIN.fullmatch(source) is None:
                            raise ValueError(f"Unpinned COPY source {source}: {path}")
                        images.append(source)
        if fields[0].upper() == "ARG" and not stages and len(fields) == 2:
            name, _, value = fields[1].partition("=")
            arguments[name] = value
        if fields[0].upper() != "FROM":
            continue
        fields = fields[1:]
        if fields[0].startswith("--platform="):
            fields = fields[1:]
        reference = fields[0]
        if reference == "${ENGINE}-image":
            if not {"terraform-image", "opentofu-image"} <= stages:
                raise ValueError(f"Unknown engine stages: {path}")
        elif reference not in stages and reference != "scratch":
            variable = re.fullmatch(r"\$\{(BASE|BASE_IMAGE)\}", reference)
            if variable:
                if variable[1] not in arguments:
                    raise ValueError(f"Undeclared parent image: {path}")
                default = arguments[variable[1]]
                if default not in {"", "scratch", "maf-image-scan:proxy-base"}:
                    if PIN.fullmatch(default) is None:
                        raise ValueError(f"Unpinned default parent: {path}")
                    images.append(default)
            elif PIN.fullmatch(reference):
                images.append(reference)
            else:
                raise ValueError(f"Unpinned or unsupported external image {reference}: {path}")
        if len(fields) == 3 and fields[1].upper() == "AS":
            stages.add(fields[2])
    return images


def dockerfiles(root: Path) -> list[Path]:
    """Include non-default Dockerfile names and package-owned fixtures."""
    return sorted(
        path
        for parent in ("images", "packages")
        for path in (root / parent).rglob("*")
        if path.is_file()
        and "dockerfile" in path.name.lower()
        and not path.name.endswith(".dockerignore")
    )


def build_inputs(root: Path) -> list[Path]:
    """Retain native dependency manifests and non-default recipes in release evidence."""
    manifests = [
        path
        for parent in ("images", "packages")
        for path in (root / parent).rglob("*")
        if path.is_file()
        and (
            (parent == "images" and path.suffix in {".json", ".csproj", ".props"})
            or path.name in {"go.mod", "go.sum", "requirements.txt"}
            or (path.name == "imports.go" and path.parent.name == "dependencies")
        )
    ]
    helpers = [
        root / "scripts" / name
        for name in (
            "container_image_inputs.py",
            "proxy_dependency_imports.go",
            "bicep_source.py",
            "build_bicep_image.py",
            "build_bicep_prepared_image.py",
            "build_scan_image.sh",
            "build_wolfi_python.py",
        )
        if (root / "scripts" / name).is_file()
    ]
    python_inputs = [
        path
        for path in (root / "images/wolfi-python").glob("*")
        if path.is_file() and path.name != "README.md"
    ]
    return sorted(set(dockerfiles(root) + manifests + helpers + python_inputs))


def validate_version_line(reference: str, path: Path) -> None:
    """Require explicit review when a dependency changes its compatibility family."""
    pin = PIN.fullmatch(reference)
    if pin is None:
        raise ValueError("Expected a tagged digest")
    lines = {
        "cgr.dev/chainguard/wolfi-base": r"latest",
        "python": r"3\.13(?:\.\d+)?-slim-trixie",
        "golang": r"1\.26\.\d+-alpine" if "packages" in path.parts else r"1\.27\.\d+-alpine",
        "mcr.microsoft.com/dotnet/sdk": r"10\.0\.\d+",
        "alpine": r"3\.24(?:\.\d+)?",
        "mcr.microsoft.com/azurelinux/base/core": r"3\.0",
        "debian": r"bookworm-slim",
    }
    if pin["name"] not in lines or not re.fullmatch(lines[pin["name"]], pin["tag"]):
        raise ValueError(f"Review the image compatibility line: {reference} in {path}")


def main() -> None:
    """Print the source declarations for review and retained build evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    records = {p.relative_to(args.root).as_posix(): inventory(p) for p in dockerfiles(args.root)}
    for name, references in records.items():
        for reference in references:
            validate_version_line(reference, Path(name))
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
