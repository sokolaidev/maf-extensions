"""Record a host-built dependency tree; run during a derived image build, never during analysis."""

import hashlib
import json
from pathlib import Path


def main() -> None:
    """Write the complete inventory and print the digest the host supplies at attachment."""
    install = Path("/opt/maf-shell")
    root = install / "dependencies"
    inventory: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("dependency symlinks are unsupported")
        if path.is_file():
            inventory[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    raw = (json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (install / "dependencies.json").write_bytes(raw)
    print(hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    main()
