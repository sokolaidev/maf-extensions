"""Build the file-profile kernel using the qualified stream builder workflow."""

from scripts.experiments.mxc_files_patch.kernel_patch import metadata, overlay
from scripts.experiments.mxc_streams_patch import build_kernel as baseline


def main() -> int:
    """Select the combined pinned kernel layer in this dedicated build process."""
    baseline.metadata = metadata
    baseline.overlay = overlay
    return baseline.main()


if __name__ == "__main__":
    raise SystemExit(main())
