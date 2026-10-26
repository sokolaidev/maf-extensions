"""ACAS qualification rejects missing execution, ambiguous images and surviving sandboxes."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qualify_shell_acas as check  # noqa: E402

REFERENCE = "ghcr.io/sokolaidev/maf-extensions/shell@sha256:" + "a" * 64


@pytest.mark.parametrize("value", ["shell:0.1.0", "disk-id", REFERENCE[:-1], REFERENCE.upper()])
def test_only_the_published_digest_is_admitted(value):
    with pytest.raises(ValueError, match="registry digest"):
        check.image_reference(value)
    assert check.image_reference(REFERENCE) == REFERENCE


def test_absence_requires_an_observed_identity():
    with pytest.raises(ValueError, match="No acquired"):
        asyncio.run(check.require_absent(None, []))


@pytest.mark.parametrize("closed", [True, False])
def test_acquisition_observes_the_physical_instance_and_rejects_open_egress(monkeypatch, closed):
    acquire = AsyncMock(return_value=SimpleNamespace(instance_id="physical"))
    get = AsyncMock()
    monkeypatch.setattr(check.AcasSandboxBackend, "acquire", acquire)
    backend = object.__new__(check.ObservedBackend)
    backend.observed = []
    backend.client = SimpleNamespace(get_sandbox_client=lambda identity: SimpleNamespace(get=get))
    spec = SimpleNamespace(egress=check.Egress.CLOSED if closed else check.Egress.UNRESTRICTED)
    if closed:
        asyncio.run(backend.acquire(None, spec))
        assert backend.observed == ["physical"]
        get.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match="closed egress"):
            asyncio.run(backend.acquire(None, spec))
        acquire.assert_not_awaited()


@pytest.mark.parametrize("outcome", [None, HttpResponseError("denied"), ResourceNotFoundError()])
def test_only_a_service_404_proves_disposal(monkeypatch, outcome):
    get = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock()
    client = SimpleNamespace(get_sandbox_client=lambda _: SimpleNamespace(get=get))
    monkeypatch.setattr(check.asyncio, "sleep", AsyncMock())
    if isinstance(outcome, ResourceNotFoundError):
        asyncio.run(check.require_absent(client, ["owned"]))
        assert get.await_count == 1
    elif isinstance(outcome, HttpResponseError):
        with pytest.raises(HttpResponseError):
            asyncio.run(check.require_absent(client, ["owned"]))
        assert get.await_count == 1
    else:
        with pytest.raises(ValueError, match="remains"):
            asyncio.run(check.require_absent(client, ["owned"]))
        assert get.await_count == 12


@pytest.mark.parametrize("failure", [None, "result", "source", "cleanup", "ambiguous", "missing"])
def test_qualification_requires_results_unchanged_sources_and_cleanup(monkeypatch, failure):
    for key in ("ENDPOINT", "SUBSCRIPTION_ID", "RESOURCE_GROUP", "GROUP"):
        monkeypatch.setenv("ACAS_SANDBOX_" + key, "test")
    monkeypatch.setenv("MAF_SHELL_ACAS_IMAGE", REFERENCE)
    state = SimpleNamespace(closed=False, purged=False, index=0)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_disk_images(self):
            for identity in (
                []
                if failure == "missing"
                else ["one", "two"]
                if failure == "ambiguous"
                else ["one"]
            ):
                yield SimpleNamespace(id=identity, image=SimpleNamespace(base=REFERENCE))

    class Backend:
        isolation = check.AcasSandboxBackend.isolation

        def __init__(self, *args):
            self.observed = []
            state.backend = self

        async def aclose(self):
            state.closed = True

    class Router:
        def __init__(self, *args, **kwargs):
            pass

        async def dispose_scope(self, *args):
            state.purged = True

    def tools(router, store, *args, **kwargs):
        assert kwargs["image"] == REFERENCE
        assert kwargs["image_id"] == "one"

        async def invoke(**kwargs):
            state.backend.observed.append(f"sandbox-{state.index}")
            expected = check.CASES[state.index][-1]
            state.index += 1
            if failure == "source":
                store.files.clear()
            return [
                SimpleNamespace(text="bad" if failure == "result" else check.COMPLETED_TEXT),
                SimpleNamespace(text=f"Result: {expected}"),
            ]

        return [SimpleNamespace(func=invoke)]

    absent = AsyncMock(side_effect=ValueError("cleanup") if failure == "cleanup" else None)
    monkeypatch.setattr(check, "AzureCliCredential", lambda: Client())
    monkeypatch.setattr(check, "SandboxGroupClient", lambda **kwargs: Client())
    monkeypatch.setattr(check, "ObservedBackend", Backend)
    monkeypatch.setattr(check, "SandboxRouter", Router)
    monkeypatch.setattr(check, "make_shell_tools", tools)
    monkeypatch.setattr(check, "require_absent", absent)
    report = {"passed": False, "cases": []}
    if failure:
        with pytest.raises(ValueError):
            asyncio.run(check.qualify(report))
        assert report["passed"] is False
    else:
        asyncio.run(check.qualify(report))
        assert report["passed"] is True
        assert len(report["cases"]) == 5
        assert absent.await_count == 6
    if failure not in ("missing", "ambiguous"):
        assert state.closed and state.purged
