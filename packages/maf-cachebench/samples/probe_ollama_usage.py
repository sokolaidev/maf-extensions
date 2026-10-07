"""Probe what an Ollama endpoint actually reports about prompt caching.

The `ollama` Python SDK's `ChatResponse` is a closed pydantic model (extra fields are
dropped), so any cache field the server sends is discarded before the Agent Framework
client — and therefore cachebench — can see it. This probe bypasses the SDK entirely and
prints every key the server returns.

Two calls are made sharing a long, identical prefix. If the endpoint reports cache reuse
at all, the second call is where it shows up.

    # local daemon (proxies :cloud models through your signed-in session)
    python probe_ollama_usage.py glm-5.2:cloud

    # direct Ollama Cloud API — the route a deployed application uses.
    # Note the model name drops the ":cloud" suffix on this endpoint.
    OLLAMA_HOST=https://ollama.com OLLAMA_API_KEY=... python probe_ollama_usage.py glm-5.2

Pass --openai to probe the OpenAI-compatible surface (/v1/chat/completions) instead of
the native /api/chat one; cached counts would arrive there as
`usage.prompt_tokens_details.cached_tokens`.
"""

import json
import os
import sys

import httpx

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
API_KEY = os.environ.get("OLLAMA_API_KEY")
USE_OPENAI = "--openai" in sys.argv[1:]
POSITIONAL = [arg for arg in sys.argv[1:] if arg != "--openai"]
MODEL = POSITIONAL[0] if POSITIONAL else "glm-5.2:cloud"

# Long enough to clear any plausible minimum cacheable prefix.
PREFIX = "You are a systems engineering assistant. Reference notes: " + (
    "pipeline schema migration cluster latency throughput retry checkpoint partition index " * 90
)


def _post(path: str, payload: dict) -> dict:
    response = httpx.post(
        f"{HOST}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {API_KEY}"} if API_KEY else None,
        timeout=300.0,
    )
    response.raise_for_status()
    return response.json()


def _call(user: str) -> dict:
    messages = [{"role": "system", "content": PREFIX}, {"role": "user", "content": user}]
    if USE_OPENAI:
        return _post(
            "/v1/chat/completions",
            {
                "model": MODEL,
                "messages": messages,
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            },
        )
    return _post(
        "/api/chat",
        {
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": 8, "temperature": 0},
        },
    )


def main() -> int:
    """Run both calls and report every usage key the endpoint returned."""
    surface = "/v1/chat/completions" if USE_OPENAI else "/api/chat"
    print(
        f"host={HOST}  model={MODEL}  surface={surface}  auth={'bearer' if API_KEY else 'none'}\n"
    )

    seen: set[str] = set()
    for label, user in (("cold prefix", "Say OK."), ("warm prefix", "Say OK again.")):
        try:
            body = _call(user)
        except httpx.HTTPStatusError as error:
            print(f"{label}: HTTP {error.response.status_code} {error.response.text[:300]!r}")
            return 1
        except httpx.HTTPError as error:
            print(f"{label}: {error}")
            return 1

        reported = (
            (body.get("usage") or {})
            if USE_OPENAI
            else {key: value for key, value in body.items() if key != "message"}
        )
        seen |= set(reported)
        print(f"=== {label} ===")
        print(json.dumps(reported, indent=2))
        print()

    cache_like = sorted(k for k in seen if "cach" in k.lower())
    print("cache-related keys reported:", cache_like or "NONE")
    if not USE_OPENAI:
        print("(re-run with --openai to check usage.prompt_tokens_details on the OpenAI surface)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
