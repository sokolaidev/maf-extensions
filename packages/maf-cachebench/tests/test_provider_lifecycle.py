"""Provider ownership spans every cell and survives early exits."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from maf_cachebench._providers import ConfiguredClient, ProviderRuntime, build_provider

pytestmark = pytest.mark.anyio


class Resource:
    def __init__(self, name: str, events: list[str], *, fails: bool = False) -> None:
        self.name, self.events, self.fails = name, events, fails

    async def close(self) -> None:
        self.events.append(self.name)
        if self.fails:
            raise RuntimeError(self.name)


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_owned_runtime_closes_on_success_error_and_cancellation(failure):
    events: list[str] = []
    client = Resource("client", events)
    credential = Resource("credential", events)
    runtime = ProviderRuntime(client, "stub", owns_client=True, owned_resources=(credential,))

    async def run():
        async with runtime:
            events.extend(["cell one", "cell two"])
            if failure:
                raise failure("stop")

    if failure:
        with pytest.raises(failure):
            await run()
    else:
        await run()
    await runtime.close()
    assert events == ["cell one", "cell two", "client", "credential"]
    with pytest.raises(RuntimeError, match="closed"):
        async with runtime:
            pass


async def test_cleanup_failure_still_closes_other_owned_resources_once():
    events: list[str] = []
    client = Resource("client", events, fails=True)
    credential = Resource("credential", events)
    runtime = ProviderRuntime(
        client, "stub", owns_client=True, owned_resources=(credential, client)
    )
    with pytest.raises(RuntimeError, match="client"):
        await runtime.close()
    await runtime.close()
    assert events == ["client", "credential"]


async def test_manually_supplied_client_is_borrowed_by_default():
    events: list[str] = []
    async with ProviderRuntime(Resource("borrowed", events), "stub"):
        pass
    assert events == []


async def test_configured_client_forwards_context_and_request_options():
    events: list[str] = []

    class Client:
        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *args):
            events.append("exit")

        async def get_response(self, **kwargs):
            events.append("call")
            return kwargs

    async with ConfiguredClient(Client(), {"store": False}) as client:
        assert await client.get_response(options={"max_tokens": 3}) == {
            "options": {"store": False, "max_tokens": 3}
        }
    assert events == ["enter", "call", "exit"]


@pytest.mark.parametrize(
    "provider", ["azure", "azure-responses", "foundry", "openrouter", "mistral", "ollama"]
)
async def test_real_provider_factories_own_and_close_their_resources(monkeypatch, provider):
    for key, value in {
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "test-key",
        "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "OPENROUTER_API_KEY": "test-key",
        "MISTRAL_API_KEY": "test-key",
        "OLLAMA_HOST": "http://localhost:11434",
    }.items():
        monkeypatch.setenv(key, value)
    runtime = build_provider(provider, temperature=None, response_max_tokens=8, model="test-model")
    assert runtime.owns_client
    client_close = AsyncMock(wraps=runtime.client.close)
    monkeypatch.setattr(runtime.client, "close", client_close)
    extra_closes: list[Any] = []
    for resource in runtime.owned_resources:
        name = "aclose" if hasattr(resource, "aclose") else "close"
        close = AsyncMock(wraps=getattr(resource, name))
        monkeypatch.setattr(resource, name, close)
        extra_closes.append(close)
    async with runtime:
        pass
    await runtime.close()
    client_close.assert_awaited_once()
    for close in extra_closes:
        close.assert_awaited_once()
    assert len(extra_closes) == (1 if provider in {"azure-responses", "foundry", "ollama"} else 0)


async def test_owned_framework_wrapper_does_not_close_a_borrowed_sdk_client():
    from agent_framework_openai import OpenAIChatClient
    from openai import AsyncOpenAI

    async with AsyncOpenAI(api_key="unused") as sdk:
        wrapper = OpenAIChatClient(model="test", async_client=sdk)
        async with ProviderRuntime(wrapper, "test", owns_client=True):
            pass
        assert not sdk.is_closed()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_matrix_closes_shared_runtime_when_a_cell_fails(monkeypatch, failure):
    from maf_cachebench import _cli
    from maf_cachebench._tokenizers import build_tokenizer

    events: list[str] = []
    runtime = ProviderRuntime(Resource("closed", events), "stub", owns_client=True)
    monkeypatch.setattr(_cli, "build_provider", lambda *args, **kwargs: runtime)

    async def fail(**kwargs):
        events.append("cell")
        raise failure("stop")

    monkeypatch.setattr(_cli, "run_cell", fail)
    args = _cli.build_parser().parse_args([])
    args.run_id = "lifecycle"
    with pytest.raises(failure):
        await _cli._run_matrix(
            args,
            tokenizer=build_tokenizer("estimator"),
            providers=["azure"],
            sizes=["small"],
            strategies=["none"],
            on_record=None,
        )
    assert events == ["cell", "closed"]
