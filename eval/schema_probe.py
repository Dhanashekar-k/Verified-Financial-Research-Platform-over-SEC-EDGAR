"""ONE Haiku structured-output call in record mode: proves transform_schema round-trips.

Uses a nested schema with a default field (the shapes that need additionalProperties false
on $defs objects and default-moving). Recorded 2026-11-23 (UTC) into
eval/cassettes/schema_probe/; tests replay it at $0.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from app.llm.client import LLMClient
from app.llm.types import LLMMode


class Fact(BaseModel):
    label: str
    value: float


class Probe(BaseModel):
    company: str
    facts: list[Fact]
    fiscal_year: int = 2023


SYSTEM = "Extract the requested fields as JSON matching the schema."
MESSAGES = [
    {
        "role": "user",
        "content": "Apple reported revenue of 383.3 and net income of 97.0 "
        "(USD billions) for fiscal 2023.",
    }
]


def main() -> int:
    """Usage: python -m eval.schema_probe <cassette_dir>  (ONE paid Haiku call, record mode)."""
    out = Path(sys.argv[1])
    client = LLMClient(mode=LLMMode.record, cassette_dir=out, max_retries=0)
    obj, usage = client.structured("planner", SYSTEM, MESSAGES, Probe, max_tokens=512)
    print("OBJECT", obj.model_dump_json())
    print("USAGE", usage.model_dump_json())
    print("CASSETTES", [p.name for p in out.glob("*.json")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
