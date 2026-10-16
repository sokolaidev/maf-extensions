"""Fixed source identities shared by live probes and offline evaluation."""

import hashlib
import json

SIMPLE_SOURCE = "output greeting string = 'hello'"
LARGE_SOURCE = "\n".join(f"var v{i} = range(0, 1000)" for i in range(1500))
PREPARED_SOURCE = "module network 'br/public:avm/res/network/virtual-network:0.7.2' = {\n name: 'network'\n params: { name: 'example-network'\n addressPrefixes: ['10.0.0.0/16'] }\n}"


def source_digest(source: str) -> str:
    """Hash the fixed single-file project using the Bicep snapshot contract."""
    return hashlib.sha256(
        json.dumps([["main.bicep", source]], separators=(",", ":")).encode()
    ).hexdigest()
