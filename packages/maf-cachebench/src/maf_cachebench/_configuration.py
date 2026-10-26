"""Serializable controls for recall, summaries and session lifecycle experiments."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, fields
from math import isfinite
from typing import Any

import maf_compaction


@dataclass(frozen=True, slots=True)
class CompactionOptions:
    """Controls shared by the live runner, its CLI and recorded cell identity."""

    preserve_prompt_cache: bool = False
    record_instruction: str | None = None
    supports_tool_choice: bool = True
    forced_call_timeout_seconds: float | None = None
    min_groups_before_repeat: int = 4
    max_summary_input_tokens: int | None = None
    summary_timeout_seconds: float | None = None
    summary_input_policy: str = "skip"
    summary_options_json: str = "{}"
    retry_without_summary_options: bool = False
    chunk_cache_size: int = 128
    max_consecutive_summary_failures: int | None = None
    summary_backoff_seconds: float | None = None
    overflow_policy: str = "shed"
    estimate_margin: float = 0.0
    user_summary_prompt: str | None = None
    user_fold_prompt: str | None = None
    record_merge_prompt: str | None = None
    session_mode: str = "memory"
    history_mode: str = "incremental"
    stream: bool = False

    def __post_init__(self) -> None:
        """Reject invalid controls before a provider is contacted."""
        for name in (
            "forced_call_timeout_seconds",
            "min_groups_before_repeat",
            "max_summary_input_tokens",
            "summary_timeout_seconds",
            "max_consecutive_summary_failures",
            "summary_backoff_seconds",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive and finite, or None.")
        if type(self.chunk_cache_size) is not int or self.chunk_cache_size < 0:
            raise ValueError("chunk_cache_size must be a non-negative integer.")
        if (self.max_consecutive_summary_failures is None) != (
            self.summary_backoff_seconds is None
        ):
            raise ValueError(
                "Set max_consecutive_summary_failures and summary_backoff_seconds together."
            )
        for name, choices in (
            ("summary_input_policy", ("skip", "chunk")),
            ("overflow_policy", ("shed", "raise")),
            ("session_mode", ("memory", "roundtrip", "restart")),
            ("history_mode", ("incremental", "replay")),
        ):
            if getattr(self, name) not in choices:
                raise ValueError(f"{name} must be one of {choices}.")
        if not isfinite(self.estimate_margin) or not 0 <= self.estimate_margin < 1:
            raise ValueError("estimate_margin must be finite and in [0, 1).")
        options = json.loads(self.summary_options_json)
        if not isinstance(options, dict):
            raise ValueError("summary_options_json must contain a JSON object.")
        object.__setattr__(
            self, "summary_options_json", json.dumps(options, sort_keys=True, allow_nan=False)
        )

    def summary_kwargs(self) -> dict[str, Any]:
        """Return the user-summary constructor controls."""
        return {
            name: getattr(self, name)
            for name in (
                "max_summary_input_tokens",
                "summary_timeout_seconds",
                "summary_input_policy",
                "retry_without_summary_options",
                "chunk_cache_size",
                "max_consecutive_summary_failures",
                "summary_backoff_seconds",
            )
        } | {
            "summary_options": json.loads(self.summary_options_json),
            "prompt": self.user_summary_prompt,
            "fold_prompt": self.user_fold_prompt,
        }


def add_compaction_arguments(parser: argparse.ArgumentParser) -> None:
    """Expose the serialized controls without a separate set of CLI defaults."""
    group = parser.add_argument_group("compaction behavior and session lifecycle")
    help_text = {
        "preserve_prompt_cache": "Keep recall guidance stable across ordinary and forced turns.",
        "record_instruction": "Custom recall instruction sent to the model.",
        "supports_tool_choice": "Whether the model supports forcing a named recall tool.",
        "forced_call_timeout_seconds": "Timeout for a forced recall attempt.",
        "min_groups_before_repeat": "Minimum new tool groups before another recall record.",
        "max_summary_input_tokens": "Maximum input tokens in one user-summary request.",
        "summary_timeout_seconds": "Timeout for one user-summary request.",
        "summary_input_policy": "Skip or chunk user-summary input exceeding its limit.",
        "summary_options_json": "JSON object of model options for user summaries.",
        "retry_without_summary_options": "Retry a rejected summary request without its options.",
        "chunk_cache_size": "Maximum cached summary chunks; zero disables caching.",
        "max_consecutive_summary_failures": "Failures before backoff; requires its duration.",
        "summary_backoff_seconds": "Summary backoff duration; requires a failure threshold.",
        "overflow_policy": "Shed context or raise when the composed budget cannot be met.",
        "estimate_margin": "Composed token-estimate safety margin in [0, 1).",
        "user_summary_prompt": "Custom user-summary prompt.",
        "user_fold_prompt": "Custom prompt for folding remembered requests.",
        "record_merge_prompt": "Custom prompt for merging recall records.",
        "session_mode": "Keep objects, round-trip JSON, or also rebuild objects each request.",
        "history_mode": "Send each new input or replay the original conversation.",
        "stream": "Consume complete streaming responses, including persistence cleanup.",
    }
    choices = {
        "summary_input_policy": ("skip", "chunk"),
        "overflow_policy": ("shed", "raise"),
        "session_mode": ("memory", "roundtrip", "restart"),
        "history_mode": ("incremental", "replay"),
    }
    defaults = CompactionOptions()
    for field in fields(defaults):
        default = getattr(defaults, field.name)
        flag = "--" + field.name.replace("_", "-")
        kwargs: dict[str, Any] = {"default": default, "help": help_text[field.name]}
        if field.name in choices:
            kwargs["choices"] = choices[field.name]
        if isinstance(default, bool):
            kwargs["action"] = argparse.BooleanOptionalAction
        elif field.name.endswith("seconds") or field.name == "estimate_margin":
            kwargs["type"] = float
        elif field.name in {
            "min_groups_before_repeat",
            "max_summary_input_tokens",
            "chunk_cache_size",
            "max_consecutive_summary_failures",
        }:
            kwargs["type"] = int
        else:
            kwargs["type"] = str
        group.add_argument(flag, **kwargs)


def compaction_options(args: argparse.Namespace) -> CompactionOptions:
    """Read all controls from one parsed command line."""
    return CompactionOptions(
        **{field.name: getattr(args, field.name) for field in fields(CompactionOptions)}
    )


def validate_session_support(options: CompactionOptions, *, has_recall: bool) -> None:
    """Reject unsupported persistence experiments before any comparison row makes calls."""
    if options.session_mode != "memory" and has_recall:
        if not hasattr(maf_compaction, "SessionPersistence"):
            raise ValueError(
                "Session experiments with recall require maf-compaction's SessionPersistence "
                "API; install the release containing it or use --session-mode memory."
            )
