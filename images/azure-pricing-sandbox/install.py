"""Install only the pinned Azure MCP executable and license from its verified archive."""

import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path


def main() -> None:
    """Verify the complete archive before reading its two fixed members."""
    manifest = json.loads(Path(__file__).with_name("image.json").read_text("utf-8"))
    version = manifest["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-beta\.[0-9]+)?", version):
        raise ValueError("Invalid Azure MCP version")
    url = f"https://registry.npmjs.org/@azure/mcp-linux-x64/-/mcp-linux-x64-{version}.tgz"
    if manifest["url"] != url:
        raise ValueError("Azure MCP must come from the fixed npm package")
    # The URL is fixed to HTTPS npm; the archive must also match its authenticated digest.
    with urllib.request.urlopen(url, timeout=120) as response:  # nosec B310
        archive = response.read(128 * 1024 * 1024 + 1)
    if hashlib.sha512(archive).hexdigest() != manifest["sha512"]:
        raise ValueError("Azure MCP archive integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member, destination in (
            ("package/dist/azmcp", Path("/out/azmcp")),
            ("package/LICENSE", Path("/out/LICENSE")),
        ):
            entry = bundle.getmember(member)
            if not entry.isfile() or entry.size > 200 * 1024 * 1024:
                raise ValueError("Invalid Azure MCP archive member")
            stream = bundle.extractfile(entry)
            if stream is None:
                raise ValueError("Missing Azure MCP archive member")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(stream.read())
    Path("/out/azmcp").chmod(0o755)


if __name__ == "__main__":
    main()
