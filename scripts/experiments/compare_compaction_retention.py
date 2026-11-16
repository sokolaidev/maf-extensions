"""Run cachebench live with historical or hardened prompts and a matched scenario salt."""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from maf_cachebench import _live_cli
from maf_cachebench._providers import ProviderRuntime, build_provider
from maf_compaction import _composed, _usersummary


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
        if args.variant == "baseline":
            baseline = json.loads(
                Path(__file__)
                .with_name("compaction_prompt_baseline.json")
                .read_text(encoding="utf-8")
            )["prompts"]
            for module, name, value in (
                (_usersummary, "DEFAULT_USER_SUMMARY_PROMPT", baseline["summary"]),
                (_usersummary, "DEFAULT_USER_FOLD_PROMPT", baseline["fold"]),
                (_composed, "DEFAULT_RECORD_MERGE_PROMPT", baseline["merge"]),
                (_composed, "harder_record_prompt", lambda attempt: baseline[f"rewrite_{attempt}"]),
                (_live_cli, "new_run_id", lambda: args.pair_id),
            ):
                stack.enter_context(patch.object(module, name, value))
        else:
            stack.enter_context(patch.object(_live_cli, "new_run_id", lambda: args.pair_id))
        forwarded = args.cachebench_args
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        return _live_cli.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
