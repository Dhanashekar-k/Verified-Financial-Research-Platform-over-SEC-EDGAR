"""The live schema probe (one Haiku call, 2026-11-23 UTC) replays from its committed cassette.

Proves at $0, on every test run, that the request the client builds today still has the same
key as the one that round-tripped against the live API (a request-shape change would miss).
"""

from __future__ import annotations

from pathlib import Path

from app.llm.client import LLMClient
from app.llm.types import LLMMode
from eval.schema_probe import MESSAGES, SYSTEM, Probe

CASSETTES = Path(__file__).resolve().parents[3] / "eval" / "cassettes" / "schema_probe"


def test_probe_replays_from_the_recorded_live_cassette() -> None:
    client = LLMClient(mode=LLMMode.replay, cassette_dir=CASSETTES)
    obj, usage = client.structured("planner", SYSTEM, MESSAGES, Probe, max_tokens=512)
    assert obj.company == "Apple"
    assert [f.label for f in obj.facts] == ["Revenue", "Net income"]
    assert usage.model == "claude-haiku-5-5" and usage.input_tokens == 515
