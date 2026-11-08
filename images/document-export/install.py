"""Install only the authenticated Mermaid bundle and its license from the pinned archive."""

import base64
import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent
pin = json.loads((root / "mermaid.json").read_text())
if not re.fullmatch(r"\d+\.\d+\.\d+", pin["version"]) or pin["url"] != (
    f"https://registry.npmjs.org/mermaid/-/mermaid-{pin['version']}.tgz"
):
    raise ValueError("Mermaid must use its versioned npm archive")
with urllib.request.urlopen(pin["url"], timeout=60) as response:  # nosec B310
    archive = response.read(64_000_001)
if len(archive) > 64_000_000:
    raise ValueError("Mermaid archive too large")
integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
if integrity != pin["integrity"]:
    raise ValueError("Mermaid archive integrity mismatch")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
    for source, destination in (
        ("package/dist/mermaid.min.js", "mermaid.min.js"),
        ("package/LICENSE", "MERMAID-LICENSE"),
        ("package/package.json", "package.json"),
    ):
        entry = package.getmember(source)
        if not entry.isfile():
            raise ValueError("Mermaid bundle member is not a file")
        content = package.extractfile(entry)
        if content is None:
            raise ValueError("Mermaid bundle member is missing")
        (root / destination).write_bytes(content.read())
