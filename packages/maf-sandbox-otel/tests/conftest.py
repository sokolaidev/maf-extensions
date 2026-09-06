"""Skip the recorder's suite where no OpenTelemetry SDK is installed.

The package ships against the **API** alone, so the SDK is a dev dependency of this workspace
and not of the wheel. The published-cores gate installs a dependent's wheel and its declared
dependencies into a clean environment and runs this suite there, which is exactly the
environment that has no SDK — and a suite may not assert one is present, the rule
`maf-sandbox-codeact`'s e2e module follows for a sibling backend.

Nothing is lost by skipping here: what the wheel does on its own, against the API's no-op
providers, is `scripts/smoke_install.py`'s question and is asked per package in its own
environment. What needs an SDK is reading back what the recorder actually wrote, which is
what this suite is for.
"""

from __future__ import annotations

import importlib.util

collect_ignore = [] if importlib.util.find_spec("opentelemetry.sdk") else ["test_otel_observer.py"]
