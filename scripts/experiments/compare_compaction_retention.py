"""Run cachebench live with historical or hardened prompts and a matched scenario salt."""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch

from compaction_prompt_baseline import BASELINE_PROMPTS
from maf_cachebench import _live_cli
from maf_cachebench._providers import ProviderRuntime, build_provider
from maf_compaction import _composed, _usersummary
from maf_compaction._summary_input import PROMPT_VERSION_ENV, versioned_prompt


@contextmanager
def prompt_policy(variant: str, *, prefix: str = "") -> Iterator[dict[str, str]]:
    """Select only prompt text, optionally adding a stable benchmark cache namespace."""
    original_rewrite = _composed.harder_record_prompt
    current = {
        "summary": _usersummary.DEFAULT_USER_SUMMARY_PROMPT,
        "fold": _usersummary.DEFAULT_USER_FOLD_PROMPT,
        "merge": _composed.DEFAULT_RECORD_MERGE_PROMPT,
        "rewrite_1": original_rewrite(1),
        "rewrite_2": original_rewrite(2),
    }
    selected = {
        key: prefix + value
        for key, value in (
            BASELINE_PROMPTS
            if variant == "baseline"
            else {key: versioned_prompt(value, "2") for key, value in current.items()}
        ).items()
    }

    def rewrite(attempt: int) -> str:
        key = f"rewrite_{attempt}"
        if key in selected:
            return selected[key]
        return prefix + versioned_prompt(
            original_rewrite(attempt), "1" if variant == "baseline" else "2"
        )

    with ExitStack() as stack:
        # Each selected string already contains its complete prompt policy.
        stack.enter_context(patch.dict(os.environ, {PROMPT_VERSION_ENV: "1"}))
        for module, name, value in (
            (_usersummary, "DEFAULT_USER_SUMMARY_PROMPT", selected["summary"]),
            (_usersummary, "DEFAULT_USER_FOLD_PROMPT", selected["fold"]),
            (_composed, "DEFAULT_RECORD_MERGE_PROMPT", selected["merge"]),
            (_composed, "harder_record_prompt", rewrite),
        ):
            stack.enter_context(patch.object(module, name, value))
        yield selected


def main() -> int:
    """Select prompts in this process, then forward the remaining arguments to cachebench."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("baseline", "hardened"), required=True)
    parser.add_argument("--pair-id", required=True, help="Use the same value for both variants.")
    parser.add_argument("cachebench_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    def configured_provider(name: str, **kwargs: Any) -> ProviderRuntime:
        if "--no-temperature" in args.cachebench_args:
            kwargs["temperature"] = None
        return build_provider(name, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(_live_cli, "build_provider", configured_provider))
        stack.enter_context(prompt_policy(args.variant))
        stack.enter_context(patch.object(_live_cli, "new_run_id", lambda: args.pair_id))
        forwarded = args.cachebench_args
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        return _live_cli.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
