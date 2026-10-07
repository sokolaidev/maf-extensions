"""Unique identities for independent benchmark measurements."""

import time
from uuid import uuid4


def new_run_id() -> str:
    """Return a timestamped identity with an independent random cache namespace."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}"
