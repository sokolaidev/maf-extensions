"""Reject ineligible image versions using read-only authoritative release history."""

from __future__ import annotations

import subprocess
from typing import Any

from container_release import empty_catalogue, validate_catalogue, validate_eligibility
from container_release_history import History


def preflight(profile: str, release_version: str, source: str, run_id: str) -> dict[str, Any]:
    """Return validated current state; reservation must check eligibility again under its lock."""
    try:
        head = History().head()
        catalogue = head.catalogue if head else empty_catalogue()
        validate_catalogue(catalogue)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(
            f"{profile}:{release_version}: Cannot validate release catalogue: {error}. "
            "Check GitHub access and security-history evidence, then retry."
        ) from error
    validate_eligibility(
        catalogue,
        {
            "profile": profile,
            "version": release_version,
            "sourceCommit": source,
            "sourceRef": "refs/heads/main",
            "attemptId": run_id,
        },
    )
    return catalogue
