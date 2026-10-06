"""Sample arguments are validated without contacting providers."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("repeats", ["0", "-1"])
async def test_narration_rejects_nonpositive_repeats_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    repeats: str,
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid repeats must fail before provider setup")

    run = namespace["run"]
    monkeypatch.setitem(run.__globals__, "build_provider", unexpected)
    args = namespace["build_parser"]().parse_args(["azure", "--repeats", repeats])
    with pytest.raises(SystemExit, match="--repeats must be greater than 0"):
        await run(args)


async def test_narration_without_completed_samples_is_not_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SAMPLES / "probe_narration.py"))
    run = namespace["run"]

    async def empty(*args: Any, **kwargs: Any) -> Any:
        return [], [], ""

    monkeypatch.setitem(
        run.__globals__,
        "build_provider",
        lambda *a, **k: SimpleNamespace(client=object(), model="stub"),
    )
    monkeypatch.setitem(run.__globals__, "_measure", empty)
    args = namespace["build_parser"]().parse_args(
        [
            "azure",
            "--repeats",
            "1",
            "--narrations",
            "neutral",
            "--placements",
            "head",
        ]
    )
    assert await run(args) == 1


@pytest.mark.parametrize(
    "argv,model,openai",
    [
        ([], "glm-5.2:cloud", False),
        (["--openai"], "glm-5.2:cloud", True),
        (["--openai", "test-model"], "test-model", True),
        (["test-model", "--openai"], "test-model", True),
        (["test-model"], "test-model", False),
    ],
)
def test_ollama_surface_flag_never_becomes_the_model(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    model: str,
    openai: bool,
) -> None:
    sent: list[tuple[str, str]] = []

    def post(url: str, *, json: dict[str, Any], **kwargs: Any) -> httpx.Response:
        sent.append((url, json["model"]))
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    monkeypatch.setattr(sys, "argv", ["probe_ollama_usage.py", *argv])
    monkeypatch.setattr(httpx, "post", post)
    namespace = runpy.run_path(str(SAMPLES / "probe_ollama_usage.py"))
    assert namespace["main"]() == 0
    suffix = "/v1/chat/completions" if openai else "/api/chat"
    assert len(sent) == 2
    assert all(url.endswith(suffix) and selected == model for url, selected in sent)
