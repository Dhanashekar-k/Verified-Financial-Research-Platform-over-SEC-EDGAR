"""API-key handling (CLAUDE.md hard rule 5): only ``EDGARQA_ANTHROPIC_API_KEY`` is ever used.

Contract: see test_client_core.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.llm.client import LLMClient
from app.llm.types import LLMMode

from .fakes import MESSAGES, SYSTEM, Answer, FakeSDK

REPO = Path(__file__).resolve().parents[3]
LLM_PKG = REPO / "app" / "llm"
KEY_ERRORS = (RuntimeError, ValueError, KeyError)


@pytest.fixture
def only_bare_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment where just the forbidden ``ANTHROPIC_API_KEY`` exists."""
    monkeypatch.delenv("EDGARQA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-used")


@pytest.mark.parametrize("mode", [LLMMode.live, LLMMode.record])
def test_bare_anthropic_key_is_not_accepted(mode: LLMMode, only_bare_key: None) -> None:
    """With only ANTHROPIC_API_KEY set and no injected SDK, live/record refuse to start.

    The error must name EDGARQA_ANTHROPIC_API_KEY so the fix is obvious. (The anthropic SDK
    would silently pick up ANTHROPIC_API_KEY itself; the client must pass its own key explicitly.)
    """
    with pytest.raises(KEY_ERRORS, match="EDGARQA_ANTHROPIC_API_KEY"):
        LLMClient(mode=mode)


def test_empty_edgarqa_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty EDGARQA_ANTHROPIC_API_KEY counts as missing."""
    monkeypatch.setenv("EDGARQA_ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(KEY_ERRORS, match="EDGARQA_ANTHROPIC_API_KEY"):
        LLMClient(mode=LLMMode.live)


def test_live_client_builds_with_edgarqa_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """EDGARQA_ANTHROPIC_API_KEY alone is sufficient (constructing makes no network call)."""
    monkeypatch.setenv("EDGARQA_ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert LLMClient(mode=LLMMode.live).mode is LLMMode.live


@pytest.mark.parametrize("mode", [LLMMode.stub, LLMMode.replay])
def test_stub_and_replay_need_no_key(mode: LLMMode, only_bare_key: None, tmp_path: Path) -> None:
    """Offline modes must construct with no key at all (CI and tests have none)."""
    assert LLMClient(mode=mode, cassette_dir=tmp_path).mode is mode


def test_injected_sdk_client_needs_no_key(only_bare_key: None) -> None:
    """When a client is injected (tests, custom transports) no key lookup happens."""
    client = LLMClient(mode=LLMMode.live, sdk_client=FakeSDK(), sleep=lambda s: None)
    obj, _ = client.structured("planner", SYSTEM, MESSAGES, Answer)
    assert isinstance(obj, Answer)


def test_no_file_under_app_llm_mentions_bare_anthropic_api_key() -> None:
    """Grep guard: every ANTHROPIC_API_KEY in app/llm/ must be preceded by EDGARQA_.

    Asserts the package exists first so this cannot pass vacuously before implementation.
    """
    assert (LLM_PKG / "client.py").is_file(), "app/llm/client.py does not exist yet"
    bare = re.compile(r"(?<!EDGARQA_)ANTHROPIC_API_KEY")
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for path in sorted(LLM_PKG.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".md", ".txt", ".yaml", ".yml", ".toml"}
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if bare.search(line)
    ]
    assert offenders == []
