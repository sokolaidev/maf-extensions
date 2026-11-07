"""Offline admission checks for the Docker export qualification host."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import Isolation
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend
from maf_sandbox_docker import DockerSandboxBackend

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import document_export_qualification as qualification


def test_container_backend_reaches_export(monkeypatch):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(DockerSandboxBackend, "create", AsyncMock(return_value=backend))
    monkeypatch.setattr(qualification, "docker", lambda *args: "sha256:fixture")
    export = AsyncMock(side_effect=RuntimeError("export reached"))
    monkeypatch.setattr(qualification, "export_pdf", export)

    with pytest.raises(RuntimeError, match="export reached"):
        asyncio.run(qualification.check("fixture"))

    export.assert_awaited_once()
