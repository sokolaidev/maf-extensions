"""Provider client construction.

Every provider is reduced to the same three things: a chat client, a model name, and a
dict of per-request options. Imports are deferred into the builders so that a run against
one provider does not require the other providers' packages to be installed.

Cache reporting differs sharply between providers, and the benchmark never assumes it.
Whether a provider reports cache statistics is determined empirically from the responses
it actually returns, so a provider that reports nothing is shown as unreported rather than
as a zero hit rate.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Final, Literal

__all__ = [
    "PROVIDER_SPECS",
    "CacheKeyMode",
    "ConfiguredClient",
    "ProviderRuntime",
    "ProviderSpec",
    "build_provider",
    "parse_provider_selector",
    "prompt_cache_key",
    "prompt_cache_key_options",
    "provider_names",
]

type CacheKeyMode = Literal["required", "optional", "unsupported"]
"""How a provider treats ``prompt_cache_key``.

``required`` means caching does not engage at all without it, so the benchmark always
sends one. ``optional`` means caching is automatic and the key only improves matching, so
it is opt-in — an older deployment may reject the unknown field.
"""


@dataclass(slots=True)
class ProviderRuntime:
    """A ready-to-use chat client together with the options each call should carry."""

    client: Any
    model: str
    options: dict[str, Any] = field(default_factory=dict[str, Any])


class ConfiguredClient:
    """Forward client calls with provider defaults, allowing per-call option overrides."""

    def __init__(self, inner: Any, options: Mapping[str, Any]) -> None:
        self.inner = inner
        self.options = deepcopy(dict(options))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        options = deepcopy(self.options)
        overrides = dict(kwargs.pop("options", None) or {})
        if "extra_body" in options and "extra_body" in overrides:
            overrides["extra_body"] = {
                **options["extra_body"],
                **overrides["extra_body"],
            }
        options.update(overrides)
        return await self.inner.get_response(*args, options=options, **kwargs)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Static description of a benchmark provider."""

    name: str
    cache_reporting: str
    cache_key_mode: CacheKeyMode
    env_vars: tuple[str, ...]
    notes: str
    builder: Callable[[float | None, int, str | None], ProviderRuntime]
    # Providers reached through the OpenAI SDK take unknown request fields via
    # ``extra_body``; native clients that declare the option take it directly.
    cache_key_via_extra_body: bool = False


def _require_env(name: str) -> str:
    """Return an environment variable's value.

    Args:
        name: Variable to read.

    Returns:
        The variable's value.

    Raises:
        RuntimeError: If the variable is unset or empty.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set to benchmark this provider.")
    return value


def _base_options(temperature: float | None, response_max_tokens: int) -> dict[str, Any]:
    """Return the options every provider shares.

    ``max_tokens`` here is the cap on an *ordinary* call, and the live runner replaces it
    per call where a different one is wanted -- the closing questions, which have to
    enumerate everything, and the recall record. It is the caller's job to pass the same
    number it reserved out of the context window when sizing the strategies: the replay
    modes discard the model's answers and replay scripted replies, so a hard cap is free
    there, but a live run's reply becomes history and is re-sent on every later turn.
    """
    options: dict[str, Any] = {"max_tokens": response_max_tokens}
    if temperature is not None:
        options["temperature"] = temperature
    return options


def _build_azure(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build an Azure OpenAI chat completions client routed through a Foundry deployment."""
    from agent_framework_openai import OpenAIChatCompletionClient

    endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    model = (
        model_override
        or os.environ.get("AZURE_OPENAI_CHAT_COMPLETION_MODEL")
        or _require_env("AZURE_OPENAI_MODEL")
    )
    client = OpenAIChatCompletionClient(
        model=model,
        azure_endpoint=endpoint,
        api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION"),
    )
    return ProviderRuntime(
        client=client, model=model, options=_base_options(temperature, response_max_tokens)
    )


def _build_foundry(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build a Foundry project client.

    A project endpoint on its own is not enough — the client requires an explicit
    credential. ``DefaultAzureCredential`` picks up an ``az login`` session locally and a
    managed identity when deployed.
    """
    from agent_framework_foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential

    model = model_override or _require_env("FOUNDRY_MODEL")
    client = FoundryChatClient(
        model=model,
        project_endpoint=_require_env("FOUNDRY_PROJECT_ENDPOINT"),
        credential=DefaultAzureCredential(),
    )
    return ProviderRuntime(
        client=client, model=model, options=_base_options(temperature, response_max_tokens)
    )


def _build_azure_responses(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build a Responses API client on an Azure OpenAI resource endpoint, with Entra auth.

    The request path the ``foundry`` route uses -- ``FoundryChatClient`` delegates to the same
    ``RawOpenAIChatClient`` -- sent to the resource instead of the project. It exists because a
    deployment can work on one and not the other: a model may answer on the resource's
    ``/openai/v1/responses`` and return HTTP 500 on the project's for every request, a bare
    "Say hi." included, while another answers on both. Keeping the API the same
    keeps the two models' cells comparable, which the chat-completions ``azure`` route would not.
    """
    from agent_framework_openai import OpenAIChatClient
    from azure.identity.aio import DefaultAzureCredential

    model = model_override or _require_env("AZURE_OPENAI_RESPONSES_MODEL")
    client = OpenAIChatClient(
        model=model,
        azure_endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
        credential=DefaultAzureCredential(),
    )
    return ProviderRuntime(
        client=client, model=model, options=_base_options(temperature, response_max_tokens)
    )


def _build_openrouter(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build an OpenRouter client.

    OpenRouter dispatches to an upstream provider that can change between requests, and
    a different upstream means a different cache. Set ``OPENROUTER_PROVIDER_ORDER`` to a
    comma-separated provider list to pin routing; without it, cross-turn cache results
    from OpenRouter carry routing noise and should be read as a lower bound.
    """
    from agent_framework_openai import OpenAIChatCompletionClient

    model = model_override or _require_env("OPENROUTER_MODEL")
    client = OpenAIChatCompletionClient(
        model=model,
        api_key=_require_env("OPENROUTER_API_KEY"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    options = _base_options(temperature, response_max_tokens)
    extra_body: dict[str, Any] = {"usage": {"include": True}}
    if order := os.environ.get("OPENROUTER_PROVIDER_ORDER"):
        extra_body["provider"] = {
            "order": [entry.strip() for entry in order.split(",") if entry.strip()],
            "allow_fallbacks": False,
        }
    options["extra_body"] = extra_body
    return ProviderRuntime(client=client, model=model, options=options)


def _build_mistral(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build a Mistral client."""
    from agent_framework_mistral import MistralChatClient

    model = model_override or os.environ.get("MISTRAL_CHAT_MODEL") or _require_env("MISTRAL_MODEL")
    client = MistralChatClient(model=model, api_key=_require_env("MISTRAL_API_KEY"))
    return ProviderRuntime(
        client=client, model=model, options=_base_options(temperature, response_max_tokens)
    )


def _build_ollama(
    temperature: float | None, response_max_tokens: int, model_override: str | None
) -> ProviderRuntime:
    """Build an Ollama client, defaulting to Ollama Cloud.

    The Agent Framework Ollama client takes no API key, so Cloud authentication is
    supplied by handing it a pre-configured ``AsyncClient`` carrying the bearer header.
    """
    from agent_framework_ollama import OllamaChatClient
    from ollama import AsyncClient

    model = model_override or _require_env("OLLAMA_MODEL")
    host = os.environ.get("OLLAMA_HOST", "https://ollama.com")
    headers: dict[str, str] = {}
    if api_key := os.environ.get("OLLAMA_API_KEY"):
        headers["Authorization"] = f"Bearer {api_key}"
    client = OllamaChatClient(model=model, client=AsyncClient(host=host, headers=headers or None))
    return ProviderRuntime(
        client=client, model=model, options=_base_options(temperature, response_max_tokens)
    )


PROVIDER_SPECS: Final[dict[str, ProviderSpec]] = {
    "azure": ProviderSpec(
        name="azure",
        cache_reporting="yes",
        cache_key_mode="optional",
        cache_key_via_extra_body=True,
        env_vars=(
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_CHAT_COMPLETION_MODEL",
        ),
        notes="Automatic caching. 1,024-token minimum; 128-token increments before GPT-5.6.",
        builder=_build_azure,
    ),
    "foundry": ProviderSpec(
        name="foundry",
        cache_reporting="unknown",
        cache_key_mode="unsupported",
        env_vars=("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL"),
        notes="Foundry project route. Cache reporting depends on the deployed model.",
        builder=_build_foundry,
    ),
    "azure-responses": ProviderSpec(
        name="azure-responses",
        cache_reporting="yes",
        cache_key_mode="unsupported",
        env_vars=("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_RESPONSES_MODEL"),
        notes=(
            "The foundry route's Responses API on the resource endpoint, Entra auth. No cache "
            "key sent, "
            "as on foundry. Reports cache writes as well as reads."
        ),
        builder=_build_azure_responses,
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        cache_reporting="yes",
        cache_key_mode="optional",
        cache_key_via_extra_body=True,
        env_vars=("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_PROVIDER_ORDER"),
        notes=(
            "Reports cached_tokens and cache_discount. Pin OPENROUTER_PROVIDER_ORDER for stable "
            "routing."
        ),
        builder=_build_openrouter,
    ),
    "mistral": ProviderSpec(
        name="mistral",
        cache_reporting="yes",
        # The cache key is optional; repeated measurements must establish cache stability.
        cache_key_mode="optional",
        env_vars=("MISTRAL_API_KEY", "MISTRAL_CHAT_MODEL"),
        notes=(
            "Automatic caching, but engages intermittently — use --repeats. Cache reads billed at "
            "10% of input."
        ),
        builder=_build_mistral,
    ),
    "ollama": ProviderSpec(
        name="ollama",
        cache_reporting="no",
        cache_key_mode="unsupported",
        env_vars=("OLLAMA_MODEL", "OLLAMA_HOST", "OLLAMA_API_KEY"),
        notes=(
            "Caches but never reports it (measured, all models). Judge by reuse% and latency only."
        ),
        builder=_build_ollama,
    ),
}


def prompt_cache_key(salt: str) -> str:
    """Return a stable, bounded cache key derived from a cell's salt.

    Every turn in a cell must send the same key for caching to accumulate, and different
    cells must send different keys or they would share a cache and contaminate each
    other. The cell salt already has exactly those properties; hashing bounds it to a
    fixed shape.

    Args:
        salt: The cell's unique salt.

    Returns:
        A deterministic key of the form ``cachebench-<32 hex chars>``.
    """
    return "cachebench-" + hashlib.sha256(salt.encode("utf-8")).hexdigest()[:32]


def prompt_cache_key_options(
    provider: str, salt: str, *, enable_optional: bool = False
) -> dict[str, Any]:
    """Return the per-call options that pin a cell to its own provider-side cache key.

    Args:
        provider: Provider name.
        salt: The cell's unique salt.

    Keyword Args:
        enable_optional: Also send the key to providers whose caching is automatic. Off by
            default because a deployment that predates the field can reject it outright.

    Returns:
        Options to merge into the request, empty when the provider takes no key.
    """
    spec = PROVIDER_SPECS[provider]
    if spec.cache_key_mode == "unsupported":
        return {}
    if spec.cache_key_mode == "optional" and not enable_optional:
        return {}
    key = prompt_cache_key(salt)
    if spec.cache_key_via_extra_body:
        return {"extra_body": {"prompt_cache_key": key}}
    return {"prompt_cache_key": key}


def provider_names() -> list[str]:
    """Return every selectable provider name."""
    return list(PROVIDER_SPECS)


def parse_provider_selector(selector: str) -> tuple[str, str | None]:
    """Split a ``provider`` or ``provider:model`` selector.

    Comparing two models on the same provider is a first-class case — a provider's cache
    behaviour can differ sharply between model families — so the model rides in the
    selector rather than only in the environment. Only the first colon separates, because
    model identifiers legitimately contain them (``glm-5.2:cloud``, ``z-ai/glm-5.2:free``).

    Args:
        selector: Either ``"openrouter"`` or ``"openrouter:openai/gpt-5.4-mini"``.

    Returns:
        The provider name and the model override, which is ``None`` when absent.
    """
    provider, separator, model = selector.partition(":")
    return provider, (model or None) if separator else None


def build_provider(
    name: str,
    *,
    temperature: float | None,
    response_max_tokens: int,
    model: str | None = None,
) -> ProviderRuntime:
    """Construct the runtime for a named provider.

    Args:
        name: One of the keys of ``PROVIDER_SPECS``.

    Keyword Args:
        temperature: Sampling temperature, or ``None`` to omit the option entirely for
            models that reject it.
        response_max_tokens: Hard cap on generated tokens.
        model: Model identifier overriding whatever the environment supplies.

    Returns:
        The provider runtime.

    Raises:
        KeyError: If ``name`` is not a known provider.
    """
    if name not in PROVIDER_SPECS:
        raise KeyError(f"Unknown provider {name!r}. Known providers: {sorted(PROVIDER_SPECS)}")
    return PROVIDER_SPECS[name].builder(temperature, response_max_tokens, model)
