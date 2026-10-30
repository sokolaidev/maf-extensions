"""Build one validation image using the versions and artifact pins in image.json."""

import argparse
import sys
from pathlib import Path

from install import load_plan

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.build_wolfi_python import build_image  # noqa: E402


def build_command(engine: str, profile: str, tag: str | None = None) -> list[str]:
    """Derive download and image metadata arguments from the same reviewed configuration."""
    context = Path(__file__).resolve().parent
    plan = load_plan(engine, profile, context / "image.json")
    return [
        "docker",
        "build",
        "--platform",
        plan["platform"],
        "--build-arg",
        f"ENGINE={engine}",
        "--build-arg",
        f"PROFILE={profile}",
        "--build-arg",
        f"ENGINE_VERSION={plan['version']}",
        "--build-arg",
        f"PROVIDER_MANIFEST={plan['profiles'][profile] or 'image.json'}",
        "--tag",
        tag or f"{plan['image']}:{plan['version']}-{profile}",
        "--file",
        str(context / "Dockerfile"),
        str(context),
    ]


def main() -> None:
    """Use versioned tags by default, with an explicit tag override for CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["terraform", "opentofu"], default="terraform")
    parser.add_argument("--profile", default="builtin")
    parser.add_argument("--tag")
    args = parser.parse_args()
    build_image(build_command(args.engine, args.profile, args.tag))


if __name__ == "__main__":
    main()
