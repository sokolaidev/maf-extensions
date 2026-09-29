"""Samples 09, 13 and 17 reach Azure over the Responses API and a local server over chat completions.

gpt-5.6 and later refuse function tools over chat completions while reasoning is on, and all
three samples hand the model tools. Sample 19's AutoGen road is pinned in its own suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from langchain_core.messages import AIMessage

_ROOT = Path(__file__).resolve().parent.parent


def _load(directory: str) -> ModuleType:
    """`agent.py` loaded by path under a unique name, with its own directory first on the path."""
    sample = _ROOT / "samples" / directory
    name = f"_sample_{directory}"
    sys.path.insert(0, str(sample))
    try:
        spec = importlib.util.spec_from_file_location(name, sample / "agent.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(sample))
        sys.modules.pop("_scaffold", None)


sample_09 = _load("09_inprocess_bicep")
sample_13 = _load("13_bicep_fix_loop")
sample_17 = _load("17_deepagents_docker_bicep")


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_MODEL", "gpt-6-luna")


class TestSample09:
    def test_the_azure_road_is_the_responses_client(self, azure: None):
        configured = sample_09.build_client()
        assert configured is not None
        client, credential = configured
        assert type(client).__name__ == "OpenAIChatClient"
        assert credential is not None
        asyncio.run(credential.close())

    def test_the_local_road_stays_on_chat_completions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        configured = sample_09.build_client()
        assert configured is not None
        client, credential = configured
        assert type(client).__name__ == "OpenAIChatCompletionClient"
        assert credential is None

    def test_an_endpoint_without_a_model_is_reported_not_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.example.openai.azure.com")
        monkeypatch.delenv("AZURE_OPENAI_CHAT_MODEL", raising=False)
        assert sample_09.build_client() is None
        assert "AZURE_OPENAI_CHAT_MODEL" in capsys.readouterr().err


class TestSample13:
    def test_the_azure_road_is_the_responses_client(self, azure: None):
        configured = sample_13.build_client()
        assert configured is not None
        client, credential = configured
        assert type(client).__name__ == "OpenAIChatClient"
        assert credential is not None
        asyncio.run(credential.close())

    def test_the_local_road_stays_on_chat_completions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        configured = sample_13.build_client()
        assert configured is not None
        client, credential = configured
        assert type(client).__name__ == "OpenAIChatCompletionClient"
        assert credential is None


class TestSample17:
    def test_the_azure_road_uses_the_responses_api(self, azure: None):
        configured = sample_17.build_model()
        assert configured is not None
        model, credential = configured
        assert type(model).__name__ == "AzureChatOpenAI"
        assert model.use_responses_api is True  # pyright: ignore[reportAttributeAccessIssue]
        assert credential is not None
        asyncio.run(credential.close())

    def test_the_local_road_stays_on_chat_completions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        configured = sample_17.build_model()
        assert configured is not None
        model, credential = configured
        assert type(model).__name__ == "ChatOpenAI"
        assert not model.use_responses_api  # pyright: ignore[reportAttributeAccessIssue]
        assert credential is None

    def test_the_final_reply_is_text_when_the_content_is_blocks(self):
        # The Responses API returns content as a list of blocks, reasoning among them.
        reply = AIMessage(
            content=[
                {"type": "reasoning", "summary": []},
                {"type": "text", "text": "BCP018 at line 3", "annotations": []},
            ]
        )
        assert sample_17.final_reply({"messages": [reply]}) == "BCP018 at line 3"
