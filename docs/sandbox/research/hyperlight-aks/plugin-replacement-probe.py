"""Keep a guest alive across one controlled plugin replacement in a fresh proof pod."""

import json
import time
from pathlib import Path

from hyperlight_sandbox import Sandbox

sandbox = Sandbox(backend="wasm", module="python_guest.path", heap_size="400Mi", stack_size="200Mi")
result = sandbox.run("value = 41; print(value)")
assert result.exit_code == 0 and result.stdout.strip() == "41" and not result.stderr
Path("/work/plugin-restart.ready").write_text("ready")
print(
    json.dumps({"stage": "warm-guest-before-plugin-replacement", "result": "pass", "value": 41}),
    flush=True,
)
deadline = time.monotonic() + 210
while not Path("/work/plugin-restart.continue").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("plugin replacement coordination exceeded deadline")
    time.sleep(0.2)
result = sandbox.run("print(value + 1)")
assert result.exit_code == 0 and result.stdout.strip() == "42" and not result.stderr
print(
    json.dumps({"stage": "warm-guest-after-plugin-replacement", "result": "pass", "value": 42}),
    flush=True,
)
