"""Run the role-preservation fixtures against an existing Microsoft Decision deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azure.identity import AzureCliCredential

from maf_cachebench.role_preservation import CandidateRequest, CandidateVerdict, evaluate_roles

CRITERIA = {
    "faithful": "The candidate assigns the same value to the role asked about in the task.",
    "changed": "The candidate changes or removes which value the task calls for.",
    "uncertain": "There is insufficient evidence to determine whether the answer is preserved.",
}


async def run(model: str) -> dict:
    """Run one pass, retaining the native choices and token usage for every request."""
    endpoint = urlsplit(os.environ["FOUNDRY_ENDPOINT"])
    if endpoint.scheme != "https" or not (endpoint.hostname or "").endswith(
        (".services.ai.azure.com", ".cognitiveservices.azure.com")
    ):
        raise ValueError("FOUNDRY_ENDPOINT must be a public Azure Foundry resource endpoint")
    deployment = os.environ["FOUNDRY_DEPLOYMENT"]
    with AzureCliCredential() as credential:
        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
    rows = []
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=None
    ) as client:

        async def judge(request: CandidateRequest) -> CandidateVerdict:
            row = {"request": asdict(request), "started_utc": datetime.now(UTC).isoformat()}
            rows.append(row)
            payload = {
                "model": deployment,
                "state": {
                    "task": request.task,
                    "sources": request.sources,
                    "candidate": request.candidate,
                },
                "questions": {
                    "preservation": {
                        "type": "choice",
                        "instructions": request.rubric,
                        "criteria": CRITERIA,
                    }
                },
            }
            response = await client.post(
                f"https://{endpoint.hostname}/providers/microsoft/v1/systemone",
                json=payload,
            )
            row["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()
            row.update(model=data.get("model"), usage=data.get("usage"))
            if data["model"].casefold() != model.casefold() or set(data["answers"]) != {
                "preservation"
            }:
                raise ValueError("Unexpected response model or question")
            answer = data["answers"]["preservation"]
            row["native_answer"] = answer
            if answer["type"] != "choice" or answer["choice"] not in CRITERIA:
                raise ValueError("Unsupported provider verdict")
            return CandidateVerdict(answer["choice"], answer["probabilities"][answer["choice"]])

        report = await evaluate_roles(judge=judge, model_id=model)
    asked = [c for c in report.cases if c.policies["candidate_judge"].requests]
    for row, case in zip(rows, asked, strict=True):
        row.update(source=case.source, variant=case.variant, split=case.split)
    return {"report": asdict(report), "criteria": CRITERIA, "responses": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Expected underlying model identity")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output path for each run")
    result = asyncio.run(run(args.model))
    with args.output.open("x", encoding="utf-8") as handle:
        print(json.dumps(result, indent=2, allow_nan=False), file=handle)
