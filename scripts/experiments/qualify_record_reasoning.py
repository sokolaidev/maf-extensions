"""Qualify completed recall records with real provider reasoning and signature replay."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2 as httpx
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Content, Message, tool
from agent_framework._compaction import annotate_message_groups, project_included_messages
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from maf_compaction import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
)
from maf_compaction._composed import DEFAULT_RECORD_MERGE_PROMPT, _transcript
from maf_compaction._toolsummary import (
    RECALL_TOOL_NAME,
    _distinctive_tokens,
    active_record_groups,
    consolidatable_record_groups,
    make_recall_tool,
    record_bodies,
    reshapeable_record_groups,
)

VALUES = ("QUAL-ALPHA-7319", "QUAL-BETA-8243")
SYSTEM = (
    "This is a synthetic record-replay qualification. When the user asks for a record, "
    "reason through the requested arithmetic and call recall_earlier_tool_results exactly "
    "once, with no text outside the tool call. After receiving that tool's result, reply "
    "only ACK; no explanation is needed. For other requests do not call tools."
)
FOLLOW_UP = (
    "Give both QUAL identifiers from the records verbatim and calculate the greatest common "
    "divisor of 1071 and 462. Reason carefully before answering. Do not call tools."
)


def canonical(value: Any) -> str:
    """Render stable JSON for exact replay comparisons."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    """Hash an opaque value without publishing it."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def roundtrip(messages: list[Message]) -> list[Message]:
    """Restore messages through their public JSON representation."""
    return [
        Message.from_dict(item) for item in json.loads(canonical([m.to_dict() for m in messages]))
    ]


class Summary:
    """Keep exact distinct values while removing record prose for projection checks."""

    async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
        values = sorted(_distinctive_tokens(messages[-1].text or ""))
        return ChatResponse(messages=[Message("assistant", [" ".join(values)])])


def strategy() -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    """Build the production strategy with a deterministic consolidation fixture."""
    tokenizer = CharacterEstimatorTokenizer()
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=100_000, tokenizer=tokenizer
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=100_000, tokenizer=tokenizer, client=cast(Any, Summary())
        ),
    )


class Probe:
    """Compare provider bodies in memory and publish only bounded qualification evidence."""

    def __init__(self, provider: str, model: str, endpoint: str, output: Path) -> None:
        self.provider = provider
        self.model = model
        self.model_name = os.environ["QUALIFICATION_MODEL_NAME"]
        self.model_version = os.environ["QUALIFICATION_MODEL_VERSION"]
        self.script_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.output = output
        self.label = "setup"
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}
        self.credential = AzureCliCredential()
        self.http = httpx.AsyncClient(
            event_hooks={"request": [self.capture_request], "response": [self.capture_response]},
            timeout=120,
        )
        self.tools = [tool(make_recall_tool(target_tokens=None))]
        if provider == "openai":
            sdk = importlib.import_module("openai").AsyncOpenAI(
                base_url=endpoint.rstrip("/") + "/openai/v1/",
                api_key=get_bearer_token_provider(
                    self.credential, "https://cognitiveservices.azure.com/.default"
                ),
                http_client=self.http,
                max_retries=0,
            )
            self.client = importlib.import_module("agent_framework_openai").RawOpenAIChatClient(
                model=model, async_client=sdk
            )
        elif provider == "anthropic":
            sdk = importlib.import_module("anthropic").AsyncAnthropicFoundry(
                base_url=endpoint.rstrip("/") + "/anthropic/",
                azure_ad_token_provider=get_bearer_token_provider(
                    self.credential, "https://ai.azure.com/.default"
                ),
                http_client=self.http,
                max_retries=0,
            )
            self.client = importlib.import_module("agent_framework_anthropic").RawAnthropicClient(
                model=model,
                anthropic_client=sdk,
                additional_beta_flags=["thinking-binding-controls-2026-08-01"],
            )
        else:
            genai = importlib.import_module("google.genai")
            self.gemini_sdk = genai.Client(
                api_key=os.environ["GEMINI_API_KEY"],
                http_options={
                    "base_url": endpoint,
                    "api_version": "v1beta",
                    "httpx_async_client": self.http,
                    "retry_options": {"attempts": 1},
                },
            )
            self.client = self.gemini_client()

    def gemini_client(self) -> Any:
        """Use a fresh adapter so its signature cache cannot mask a replay failure."""
        return importlib.import_module("agent_framework_gemini").RawGeminiChatClient(
            model=self.model, client=self.gemini_sdk
        )

    async def capture_request(self, request: httpx.Request) -> None:
        """Retain bodies, never authentication headers or resource URLs."""
        body = json.loads(request.content)
        self.requests.append({"label": self.label, "body": body})

    async def capture_response(self, response: httpx.Response) -> None:
        """Capture response bodies for in-memory replay checks."""
        await response.aread()
        body = response.json()
        self.responses.append({"label": self.label, "status": response.status_code, "body": body})

    def wire(self, messages: list[Message]) -> Any:
        """Use the actual framework adapter's provider serialization."""
        projected = project_included_messages(messages)
        if self.provider == "openai":
            return self.client._prepare_messages_for_openai(
                projected, request_uses_service_side_storage=False
            )
        if self.provider == "gemini":
            _, contents = self.gemini_client()._prepare_gemini_messages(projected)
            models = importlib.import_module("google.genai.models")
            common = importlib.import_module("google.genai._common")
            return [
                common.encode_unserializable_types(
                    common.convert_to_dict(models._Content_to_mldev(content))
                )
                for content in contents
            ]
        return self.client._prepare_messages_for_anthropic(projected)

    async def send(self, label: str, messages: list[Message], *, finish: bool = False) -> Any:
        """Send stateless history and record service status, usage and reasoning presence."""
        self.label = label
        options: dict[str, Any] = {
            "tools": self.tools,
            "max_tokens": 4096,
            "tool_choice": "auto" if label.startswith("seed-") else "none",
        }
        if self.provider == "openai":
            options.update(store=False, reasoning={"effort": "none" if finish else "medium"})
        elif self.provider == "anthropic":
            options.update(
                thinking={
                    "type": "adaptive",
                    "block_binding": {"prefix_mismatch_behavior": "error"},
                },
                output_config={"effort": "low" if finish else "high"},
            )
        else:
            self.client = self.gemini_client()
            options.update(
                thinking_config={
                    "include_thoughts": not finish,
                    "thinking_level": "LOW" if finish else "MEDIUM",
                },
                automatic_function_calling={"disable": True},
            )
        response = await self.client.get_response(
            project_included_messages(messages), options=options
        )
        contents = [item for message in response.messages for item in message.contents]
        raw = self.responses[-1]
        expected_wire = self.wire(messages)
        history_field = {"openai": "input", "anthropic": "messages", "gemini": "contents"}[
            self.provider
        ]
        actual_wire = self.requests[-1]["body"][history_field]
        self.require(
            f"{label}_actual_wire_matches", canonical(expected_wire) == canonical(actual_wire)
        )
        if self.provider == "anthropic":
            self.require(
                f"{label}_transformations_reported", "input_transformations" in raw["body"]
            )
        transformations = raw["body"].get("input_transformations", [])
        event = {
            "label": label,
            "http_status": raw["status"],
            "finish_reason": str(response.finish_reason),
            "usage": dict(response.usage_details or {}),
            "content_types": [item.type for item in contents],
            "protected_reasoning": sum(
                item.type == "text_reasoning" and bool(item.protected_data) for item in contents
            ),
            "reasoning_items": sum(item.type == "text_reasoning" for item in contents),
            "input_transformation_count": len(transformations),
            "request_sha256": digest(self.requests[-1]["body"]),
        }
        if self.provider == "gemini":
            event["model_version"] = raw["body"].get("modelVersion")
            self.require(f"{label}_model_version", event["model_version"] == self.model_version)
            event["signed_calls"] = sum(
                bool(part.get("thoughtSignature")) and "functionCall" in part
                for candidate in raw["body"].get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
            )
        self.events.append(event)
        print(canonical(event), flush=True)
        assert not transformations, "Provider transformed retained thinking"
        return response

    def has_later_reasoning(self) -> bool:
        """Gemini carries protected signatures on calls, while text thoughts have no carrier."""
        field = "reasoning_items" if self.provider == "gemini" else "protected_reasoning"
        return self.events[-1][field] > 0

    def signatures(self, messages: list[Message]) -> dict[str, str]:
        """Index signed Gemini calls and refuse a signature attached to another part."""
        result = {}
        result_ids = set()
        for message in self.wire(messages):
            for part in message["parts"]:
                if "functionCall" in part:
                    assert part.get("thoughtSignature"), "Call lost its thought signature"
                if "functionResponse" in part:
                    result_ids.add(part["functionResponse"]["id"])
                if signature := part.get("thoughtSignature"):
                    assert "functionCall" in part, "Orphaned thought signature"
                    result[part["functionCall"]["id"]] = signature
        assert result_ids == result.keys(), "Calls and results no longer match"
        return result

    def require(self, name: str, condition: bool) -> None:
        """Record each condition before refusing an incomplete qualification."""
        self.checks[name] = condition
        assert condition, name

    async def run(self) -> None:
        """Exercise real completed records, guarded histories and session restoration."""
        history = [Message("system", [SYSTEM], message_id="qualification-system")]
        for index, value in enumerate(VALUES):
            calculation = "127 * 43 + 89 * 37" if index == 0 else "749 * 263 + 431 * 587"
            history.append(
                Message(
                    "user",
                    [
                        (
                            f"Create record {index + 1}. Check {calculation} carefully, then "
                            f"record the exact identifier {value}, the calculation and its result. "
                            "In the record values include this sentence five times: "
                            "The synthetic lookup completed and its exact findings are retained here. "
                            "Call recall_earlier_tool_results with the record; no narration."
                        )
                    ],
                    message_id=f"qualification-user-{index}",
                )
            )
            response = await self.send(f"seed-{index}", history)
            self.require(
                f"seed_{index}_protected_reasoning", self.events[-1]["protected_reasoning"] > 0
            )
            if self.provider == "gemini":
                self.require(f"seed_{index}_signed_call", self.events[-1]["signed_calls"] == 1)
            calls = [c for m in response.messages for c in m.contents if c.type == "function_call"]
            self.require(
                f"seed_{index}_one_recall", len(calls) == 1 and calls[0].name == RECALL_TOOL_NAME
            )
            call = calls[0]
            arguments = (
                json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            )
            self.require(f"seed_{index}_exact_value", value in arguments["values"])
            history.extend(response.messages)
            history.append(
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            call_id=call.call_id, result=make_recall_tool()(arguments["values"])
                        )
                    ],
                    message_id=f"qualification-result-{index}",
                )
            )
            if index == 0:
                active = copy.deepcopy(history)
                before = canonical(self.wire(active))
                self.require("active_loop_not_selected", not reshapeable_record_groups(active))
                self.require(
                    "active_loop_unchanged",
                    not strategy()._reshape_records(active)
                    and before == canonical(self.wire(active)),
                )
            if self.provider == "gemini":
                restored_active = roundtrip(history)
                self.require(
                    f"seed_{index}_signed_loop_restored",
                    canonical(self.wire(restored_active)) == canonical(self.wire(history)),
                )
                history = restored_active
            final = await self.send(f"finish-{index}", history, finish=True)
            history.extend(final.messages)
            self.require(
                f"finish_{index}_no_later_reasoning",
                not any(c.type == "text_reasoning" for m in final.messages for c in m.contents),
            )
        history.append(Message("user", [FOLLOW_UP], message_id="qualification-follow-up"))
        annotate_message_groups(history)
        self.require("two_completed_records", len(reshapeable_record_groups(history)) == 2)
        baseline = await self.send("baseline", history)
        self.require("baseline_values", all(value in baseline.text for value in VALUES))

        guarded = copy.deepcopy(history) + baseline.messages + [Message("user", ["Continue."])]
        before = canonical(self.wire(guarded))
        self.require("later_reasoning_present", self.has_later_reasoning())
        self.require("later_reasoning_not_selected", not reshapeable_record_groups(guarded))
        self.require(
            "later_reasoning_unchanged",
            not strategy()._reshape_records(guarded) and before == canonical(self.wire(guarded)),
        )
        await self.send("later-reasoning-guard", guarded)
        if self.provider == "anthropic":
            changed_prefix = copy.deepcopy(guarded)
            changed_prefix[0].contents[0].text = SYSTEM + " The prefix changed."
            try:
                await self.send("prefix-mismatch-negative", changed_prefix)
            except Exception:
                raw = self.responses[-1]
                self.require(
                    "prefix_mismatch_rejected",
                    raw["status"] == 400 and "signature" in canonical(raw["body"]).lower(),
                )
                self.events.append(
                    {
                        "label": "prefix-mismatch-negative",
                        "http_status": 400,
                        "expected_rejection": True,
                    }
                )
            else:
                self.require("prefix_mismatch_rejected", False)

        for operation in ("reshape", "merge", "rewrite"):
            current = copy.deepcopy(history)
            reducer = strategy()
            if operation == "reshape":
                self.require("reshape_applied", reducer._reshape_records(current))
            else:
                if operation == "merge":
                    self.require(
                        "merge_lossless_reshape_applied", reducer._reshape_records(current)
                    )
                groups = consolidatable_record_groups(current)
                if operation == "rewrite":
                    groups = groups[-1:]
                self.require(
                    f"{operation}_source_count", len(groups) == (2 if operation == "merge" else 1)
                )
                outcome = await reducer._consolidate(
                    current,
                    groups,
                    prompt=DEFAULT_RECORD_MERGE_PROMPT,
                    transcript=_transcript(current, groups),
                )
                self.require(f"{operation}_applied", outcome == "accepted")
            records = "\n".join(record_bodies(current, active_record_groups(current)))
            self.require(f"{operation}_record_values", all(value in records for value in VALUES))
            self.require(
                f"{operation}_wire_reduced",
                len(canonical(self.wire(current))) < len(canonical(self.wire(history))),
            )
            if self.provider == "gemini":
                original_signatures = self.signatures(history)
                retained_signatures = self.signatures(current)
                self.require(f"{operation}_two_signed_sources", len(original_signatures) == 2)
                self.require(
                    f"{operation}_call_signatures_removed_together",
                    len(retained_signatures) == (1 if operation == "rewrite" else 0),
                )
                self.require(
                    f"{operation}_surviving_signature_pairing",
                    all(
                        original_signatures[call_id] == signature
                        for call_id, signature in retained_signatures.items()
                    ),
                )
            continued = await self.send(operation, current)
            self.require(f"{operation}_later_reasoning_present", self.has_later_reasoning())
            self.require(f"{operation}_values", all(value in continued.text for value in VALUES))
            cold = strategy()
            cold.restore_session_state(json.loads(canonical(reducer.session_state())))
            restored = roundtrip(history)
            reshaped = cold._reshape_records(restored, replay=True)
            kept = cold._keep_decisions(restored)
            self.require(f"{operation}_restored", reshaped or kept)
            self.require(
                f"{operation}_restored_wire_equal",
                canonical(self.wire(restored)) == canonical(self.wire(current)),
            )
            replayed = await self.send(f"{operation}-restored", restored)
            self.require(
                f"{operation}_restored_values", all(value in replayed.text for value in VALUES)
            )
            tail = continued.messages + [Message("user", ["Repeat both QUAL identifiers."])]
            restored_later = roundtrip(history + tail)
            again = strategy()
            again.restore_session_state(json.loads(canonical(reducer.session_state())))
            reshaped = again._reshape_records(restored_later, replay=True)
            kept = again._keep_decisions(restored_later)
            self.require(f"{operation}_later_replay", reshaped or kept)
            self.require(
                f"{operation}_later_wire_equal",
                canonical(self.wire(restored_later)) == canonical(self.wire(current + tail)),
            )
            await self.send(f"{operation}-restored-later", restored_later)

    async def close(self, error: Exception | None) -> None:
        """Persist payload digests and qualification metrics, then close clients."""
        self.output.mkdir(parents=True, exist_ok=True)
        packages = (
            "agent-framework-core",
            "agent-framework-openai",
            "openai",
            "anthropic",
            "azure-identity",
        )
        history_field = {"openai": "input", "anthropic": "messages", "gemini": "contents"}[
            self.provider
        ]
        report = {
            "provider": self.provider,
            "model": self.model_name,
            "model_version": self.model_version,
            "script_sha256": self.script_sha256,
            "settings": {"store": False, "reasoning_effort": "medium", "finish_effort": "none"}
            if self.provider == "openai"
            else {
                "thinking": "adaptive",
                "prefix_mismatch_behavior": "error",
                "beta": "thinking-binding-controls-2026-08-01",
                "effort": "high",
                "finish_effort": "low",
            },
            "utc": datetime.now(UTC).isoformat(),
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "python": platform.python_version(),
            "packages": {name: importlib.metadata.version(name) for name in packages},
            "checks": self.checks,
            "events": self.events,
            "requests": [
                {
                    "label": request["label"],
                    "sha256": digest(request["body"]),
                    "history_items": len(request["body"].get(history_field, [])),
                    "tool_count": len(request["body"].get("tools", [])),
                }
                for request in self.requests
            ],
            "responses": [
                {
                    "label": response["label"],
                    "http_status": response["status"],
                    "sha256": digest(response["body"]),
                }
                for response in self.responses
            ],
            "passed": error is None,
            "error_type": type(error).__name__ if error else None,
            "error_stage": self.label if error else None,
        }
        if self.provider == "anthropic":
            report["packages"]["agent-framework-anthropic"] = importlib.metadata.version(
                "agent-framework-anthropic"
            )
        if self.provider == "gemini":
            report["settings"] = {
                "api_version": "v1beta",
                "thinking_level": "MEDIUM",
                "finish_thinking_level": "LOW",
                "include_thoughts": True,
                "finish_include_thoughts": False,
                "fresh_adapter_per_request": True,
                "automatic_function_calling": False,
            }
            for name in ("agent-framework-gemini", "google-genai"):
                report["packages"][name] = importlib.metadata.version(name)
        (self.output / f"{self.provider}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        await self.http.aclose()
        if self.provider == "gemini":
            await self.gemini_sdk.aio.aclose()
            self.gemini_sdk.close()
        await self.credential.close()


async def main() -> int:
    """Use explicitly selected deployments without recording authentication material."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("openai", "anthropic", "gemini"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prefix = {"openai": "AZURE_OPENAI", "anthropic": "ANTHROPIC_FOUNDRY", "gemini": "GEMINI"}[
        args.provider
    ]
    probe = Probe(
        args.provider, os.environ[f"{prefix}_MODEL"], os.environ[f"{prefix}_ENDPOINT"], args.output
    )
    error = None
    try:
        await probe.run()
    except Exception as exc:
        error = exc
        print(
            canonical(
                {
                    "error_type": type(exc).__name__,
                    "stage": probe.label,
                    "failed_checks": [key for key, value in probe.checks.items() if not value],
                }
            ),
            flush=True,
        )
    finally:
        await probe.close(error)
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
