"""Compare a Mistral deployment's cache hits with and without a prompt cache key.

Set MISTRAL_API_KEY and optionally pass the model name as the first argument.
"""

import json
import os
import sys
import time

import httpx

API_KEY = os.environ.get("MISTRAL_API_KEY")
MODEL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("MISTRAL_CHAT_MODEL", "mistral-small-latest")
)
URL = (
    os.environ.get("MISTRAL_SERVER_URL", "https://api.mistral.ai").rstrip("/")
    + "/v1/chat/completions"
)
ROUNDS = 3

# Comfortably above every documented minimum cacheable prefix.
PREFIX = "You are a systems engineering assistant. Reference notes: " + (
    "pipeline schema migration cluster latency throughput retry checkpoint partition index " * 600
)


def _call(user: str, cache_key: str | None) -> tuple[dict, float]:
    payload: dict = {
        "model": MODEL,
        "messages": [{"role": "system", "content": PREFIX}, {"role": "user", "content": user}],
        "max_tokens": 4,
        "temperature": 0,
        "stream": False,
    }
    if cache_key is not None:
        payload["prompt_cache_key"] = cache_key
    started = time.perf_counter()
    response = httpx.post(
        URL, json=payload, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=300.0
    )
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed


def _round(label: str, cache_key: str | None) -> bool:
    print(f"\n===== {label} =====")
    saw_cache = False
    for index in range(1, ROUNDS + 1):
        try:
            body, elapsed = _call(f"Say OK ({index}).", cache_key)
        except httpx.HTTPStatusError as error:
            print(f"  [{index}] HTTP {error.response.status_code}: {error.response.text[:300]}")
            return saw_cache
        except httpx.HTTPError as error:
            print(f"  [{index}] {type(error).__name__}: {error}")
            return saw_cache
        usage = body.get("usage") or {}
        print(f"  [{index}] wall={elapsed:.0f}ms  usage={json.dumps(usage)}")
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens"):
            saw_cache = True
    return saw_cache


def main() -> int:
    """Run both rounds and report whether prompt_cache_key made the difference."""
    if not API_KEY:
        print("MISTRAL_API_KEY must be set.")
        return 2
    print(f"url={URL}  model={MODEL}  prefix_chars={len(PREFIX)}")

    without = _round("WITHOUT prompt_cache_key", None)
    with_key = _round("WITH prompt_cache_key", "cachebench-probe-fixed-key")

    print("\n===== verdict =====")
    print(f"  cached_tokens reported without a key: {without}")
    print(f"  cached_tokens reported with a key:    {with_key}")
    if with_key and not without:
        print("  -> cache hits appeared only with a key; pass --prompt-cache-key to cachebench")
    elif with_key and without:
        print("  -> caching is automatic; the key is harmless but not required")
    elif not with_key and not without:
        print("  -> no caching reported either way; check model support and prefix size")
    else:
        print("  -> unexpected: cached only without a key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
