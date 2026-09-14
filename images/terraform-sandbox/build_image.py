"""Build one validation image using the versions and artifact pins in image.json."""

import argparse
import subprocess
from pathlib import Path

from install import load_plan


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
        f"BASE_IMAGE={plan['base_image']}",
        "--build-arg",
        f"ENGINE={engine}",
        "--build-arg",
        f"PROFILE={profile}",
        "--build-arg",
        f"ENGINE_VERSION={plan['version']}",
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
    subprocess.run(build_command(args.engine, args.profile, args.tag), check=True)


if __name__ == "__main__":
    main()
