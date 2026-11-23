"""Core behavior of ``app/llm/client.py``: mode selection, registry, stub mode, request shape,
usage accounting.

INTERFACE CONTRACT ASSUMED BY ALL TESTS IN tests/unit/llm/ (implement against this)
=================================================================================

app/llm/types.py
    class LLMMode(StrEnum): live, stub, record, replay, batch   (values are the lowercase names)
    class Usage(BaseModel): model: str; input_tokens: int; cache_write_tokens: int;
        cache_read_tokens: int; output_tokens: int; cost_usd: Decimal; latency_ms: int;
        batch: bool = False

app/llm/prices.py
    class ModelPrice: input, output, cache_read (Decimal, USD per million tokens)
    PRICES: dict[str, ModelPrice]          keys: claude-{haiku,sonnet,opus}-5-5
    cost_usd(model, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens, *,
             batch=False) -> Decimal       exact (no rounding); unknown model ->
                                           KeyError|ValueError; negative tokens -> ValueError

app/llm/stub.py
    register_stub(schema, fn)              fn() is called (any args) and must return a `schema`
                                           instance; registered stubs win over the default stub.

app/llm/client.py
    REGISTRY: dict[str, str]               planner/verifier=claude-haiku-5-5,
                                           composer/judge=claude-sonnet-5-5
    class CassetteMissError(Exception)     replay miss; message contains the request key
    class LLMClient:
        __init__(self, mode: LLMMode | None = None, cassette_dir: Path = Path("eval/cassettes"),
                 registry: dict[str, str] | None = None, sdk_client=None, *,
                 sleep=time.sleep, max_retries: int = ...)
        attributes: .mode (LLMMode), .cassette_dir (Path)
        mode=None -> env EDGARQA_LLM_MODE, falling back to live when unset.
        registry (if given) overrides the entries it names; other roles keep REGISTRY defaults.
        structured(role_or_model, system, messages, schema, *, max_tokens=2048)
            -> tuple[BaseModel, Usage]
            role_or_model: a REGISTRY role, or a concrete model id used as is.
        request_key(role_or_model, system, messages, schema, *, max_tokens=2048) -> str
            SHA-256 hex (64 chars) of canonical JSON (sorted keys) of {model (resolved from role),
            system, messages, schema.model_json_schema(), max_tokens}.

Request sent to ``sdk_client.messages.create`` (live and record modes):
    model=<resolved id>, max_tokens=<int>, messages=<as given>,
    system=[{"type": "text", "text": <system>, "cache_control": {"type": "ephemeral"}}],
    output_config={"format": {"type": "json_schema", "schema": <dict>}}
Response parsing: ``.content[0].text`` is JSON validated into ``schema``; ``.usage`` maps to
    Usage(input_tokens=input_tokens, cache_write_tokens=cache_creation_input_tokens,
          cache_read_tokens=cache_read_input_tokens, output_tokens=output_tokens), priced via
    prices.cost_usd. A non-"end_turn" stop_reason (e.g. "max_tokens" truncation) raises instead of
    returning a half-parsed object.

Retries (live and record): on anthropic.RateLimitError (429) and any 5xx
    anthropic.InternalServerError (incl. 529), sleep(delay) then retry, up to ``max_retries``
    retries after the first attempt (so max_retries=4 means at most 5 SDK calls); delays are
    positive and strictly increasing (exponential). Other API errors (400, 401, 403, 404, 422)
    are raised immediately with no sleep. When retries run out the last error is re-raised.

Modes: stub = no network, no SDK, deterministic, Usage cost 0, never needs an API key.
    replay = reads <cassette_dir>/<request_key>.json, never touches the SDK, miss ->
    CassetteMissError. record = call SDK, then write the cassette (only on a successful, parsed
    response). live = call SDK, no cassette. live/record need EDGARQA_ANTHROPIC_API_KEY (and only
    that name) unless sdk_client is injected.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.llm.client import REGISTRY, LLMClient
from app.llm.prices import cost_usd
from app.llm.stub import register_stub
from app.llm.types import LLMMode, Usage

from .fakes import (
    GOOD,
    MESSAGES,
    SYSTEM,
    Answer,
    DefaultedAnswer,
    ExplodingSDK,
    FakeSDK,
    Sleeper,
    make_response,
)


def _live(sdk: FakeSDK, tmp_path: Path, mode: LLMMode = LLMMode.live, **kw) -> LLMClient:  # type: ignore[no-untyped-def]
    return LLMClient(
        mode=mode, cassette_dir=tmp_path, sdk_client=sdk, sleep=Sleeper(), max_retries=2, **kw
    )


# --- registry ----------------------------------------------------------------------------


def test_default_registry_matches_m04_spec() -> None:
    """M04 3.1: planner/verifier on Haiku 5.5, composer/judge on Sonnet 5.5."""
    assert REGISTRY == {
        "planner": "claude-haiku-5-5",
        "composer": "claude-sonnet-5-5",
        "verifier": "claude-haiku-5-5",
        "judge": "claude-sonnet-5-5",
    }


@pytest.mark.parametrize(("role", "model"), sorted(REGISTRY.items()))
def test_role_resolves_to_registered_model_in_the_request(
    role: str, model: str, tmp_path: Path
) -> None:
    """A role name is translated to the model id sent to the API."""
    sdk = FakeSDK()
    _live(sdk, tmp_path).structured(role, SYSTEM, MESSAGES, Answer)
    assert sdk.calls[0]["model"] == model


def test_concrete_model_id_passes_through_unchanged(tmp_path: Path) -> None:
    """Passing a concrete model id bypasses the registry (Pareto runs compare models directly)."""
    sdk = FakeSDK()
    _live(sdk, tmp_path).structured("claude-opus-5-5", SYSTEM, MESSAGES, Answer)
    assert sdk.calls[0]["model"] == "claude-opus-5-5"


def test_registry_override_changes_only_the_named_role(tmp_path: Path) -> None:
    """'composer=sonnet|haiku configurable per run': override composer, planner stays Haiku."""
    sdk = FakeSDK([make_response(), make_response()])
    client = LLMClient(
        mode=LLMMode.live,
        cassette_dir=tmp_path,
        registry={"composer": "claude-haiku-5-5"},
        sdk_client=sdk,
        sleep=Sleeper(),
    )
    client.structured("composer", SYSTEM, MESSAGES, Answer)
    client.structured("judge", SYSTEM, MESSAGES, Answer)
    assert [c["model"] for c in sdk.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]


def test_registry_override_does_not_mutate_module_registry(tmp_path: Path) -> None:
    """Per-run configuration must not leak into the module-level default for later clients."""
    before = dict(REGISTRY)
    LLMClient(
        mode=LLMMode.stub, cassette_dir=tmp_path, registry={"planner": "claude-opus-5-5"}
    ).structured("planner", SYSTEM, MESSAGES, DefaultedAnswer)
    assert before == REGISTRY


# --- mode selection ----------------------------------------------------------------------


def test_default_mode_in_tests_is_stub() -> None:
    """CLAUDE.md rule 4: under the autouse EDGARQA_LLM_MODE=stub fixture a bare client is stub."""
    assert LLMClient().mode is LLMMode.stub


def test_mode_falls_back_to_live_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no EDGARQA_LLM_MODE the production default is live (sdk injected: no key needed)."""
    monkeypatch.delenv("EDGARQA_LLM_MODE", raising=False)
    assert LLMClient(sdk_client=FakeSDK()).mode is LLMMode.live


@pytest.mark.parametrize("mode", list(LLMMode))
def test_mode_is_read_from_env(mode: LLMMode, monkeypatch: pytest.MonkeyPatch) -> None:
    """EDGARQA_LLM_MODE selects the mode (sdk injected so live/record need no key)."""
    monkeypatch.setenv("EDGARQA_LLM_MODE", mode.value)
    assert LLMClient(sdk_client=FakeSDK()).mode is mode


def test_explicit_mode_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit constructor mode wins over the environment."""
    monkeypatch.setenv("EDGARQA_LLM_MODE", "live")
    assert LLMClient(mode=LLMMode.replay).mode is LLMMode.replay


def test_invalid_env_mode_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd env value must not silently fall back to live."""
    monkeypatch.setenv("EDGARQA_LLM_MODE", "lvie")
    with pytest.raises(ValueError):
        LLMClient(sdk_client=FakeSDK())


def test_default_cassette_dir_is_eval_cassettes() -> None:
    """Cassettes live in eval/cassettes by default (M04 3.1)."""
    assert LLMClient(mode=LLMMode.stub).cassette_dir == Path("eval/cassettes")


# --- stub mode ---------------------------------------------------------------------------


def test_stub_returns_instance_of_schema_with_zero_cost(tmp_path: Path) -> None:
    """Stub returns a `schema` instance and a Usage priced at exactly $0."""
    client = LLMClient(mode=LLMMode.stub, cassette_dir=tmp_path)
    obj, usage = client.structured("planner", SYSTEM, MESSAGES, DefaultedAnswer)
    assert isinstance(obj, DefaultedAnswer)
    assert isinstance(usage, Usage)
    assert usage.cost_usd == Decimal("0")
    assert usage.model == "claude-haiku-5-5"
    assert usage.batch is False


def test_stub_is_deterministic() -> None:
    """Same request twice -> equal object and equal token accounting (tests rely on this)."""
    client = LLMClient(mode=LLMMode.stub)
    a = client.structured("planner", SYSTEM, MESSAGES, DefaultedAnswer)
    b = client.structured("planner", SYSTEM, MESSAGES, DefaultedAnswer)
    assert a[0] == b[0]
    assert a[1].cost_usd == b[1].cost_usd == Decimal("0")


def test_stub_instance_validates_against_schema() -> None:
    """For an all-defaults schema the stub instance round-trips through validation."""
    obj, _ = LLMClient(mode=LLMMode.stub).structured("judge", SYSTEM, MESSAGES, DefaultedAnswer)
    assert DefaultedAnswer.model_validate(obj.model_dump()) == obj


def test_stub_never_touches_sdk_cassettes_or_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub: no SDK call, no cassette files, and no API key required."""
    for var in ("EDGARQA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    sdk = ExplodingSDK()
    client = LLMClient(mode=LLMMode.stub, cassette_dir=tmp_path, sdk_client=sdk)
    client.structured("composer", SYSTEM, MESSAGES, DefaultedAnswer)
    assert sdk.calls == []
    assert list(tmp_path.iterdir()) == []


def test_stub_without_factory_for_required_fields_fails_loudly() -> None:
    """A schema with required fields and no registered stub raises, instead of returning a
    model_construct object whose required attributes are missing."""
    with pytest.raises(LookupError, match="register_stub"):
        LLMClient(mode=LLMMode.stub).structured("planner", SYSTEM, MESSAGES, Answer)


def test_registered_stub_factory_is_used_for_its_schema() -> None:
    """register_stub(schema, fn) lets a test script the stub's answer for one schema."""

    class Plan(Answer):
        pass

    canned = Plan(value=7, label="canned")
    calls: list[tuple] = []

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return canned

    register_stub(Plan, factory)
    obj, usage = LLMClient(mode=LLMMode.stub).structured("planner", SYSTEM, MESSAGES, Plan)
    assert obj == canned
    assert len(calls) == 1
    assert usage.cost_usd == Decimal("0")


def test_registered_stub_does_not_affect_other_schemas() -> None:
    """A stub registered for one schema must not change the default stub for another."""

    class Registered(Answer):
        pass

    class Other(DefaultedAnswer):
        pass

    register_stub(Registered, lambda *a, **k: Registered(value=1, label="x"))
    obj, _ = LLMClient(mode=LLMMode.stub).structured("planner", SYSTEM, MESSAGES, Other)
    assert isinstance(obj, Other)
    assert not isinstance(obj, Registered)


# --- request shape (live and record) -----------------------------------------------------


@pytest.mark.parametrize("mode", [LLMMode.live, LLMMode.record])
def test_request_uses_structured_output_config(mode: LLMMode, tmp_path: Path) -> None:
    """Structured outputs: output_config.format = json_schema with the pydantic schema."""
    sdk = FakeSDK()
    _live(sdk, tmp_path, mode).structured("planner", SYSTEM, MESSAGES, Answer, max_tokens=512)
    (call,) = sdk.calls
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["properties"]) == {"value", "label"}
    assert call["max_tokens"] == 512
    assert call["messages"] == MESSAGES


def test_default_max_tokens_is_2048(tmp_path: Path) -> None:
    """Contract default: max_tokens=2048."""
    sdk = FakeSDK()
    _live(sdk, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert sdk.calls[0]["max_tokens"] == 2048


def test_system_prompt_is_a_cached_block_list(tmp_path: Path) -> None:
    """Prompt caching: system goes as a block list whose static block has an ephemeral breakpoint.

    Without the breakpoint the metric catalog / instructions are re-billed at full price per call.
    """
    sdk = FakeSDK()
    _live(sdk, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    system = sdk.calls[0]["system"]
    assert isinstance(system, list)
    block = system[0]
    assert block["type"] == "text"
    assert block["text"] == SYSTEM
    assert block["cache_control"] == {"type": "ephemeral"}


def test_request_never_sets_temperature(tmp_path: Path) -> None:
    """Plan docs check: a non-default temperature is a 400 on Haiku/Sonnet 5.5, so none is sent."""
    sdk = FakeSDK()
    _live(sdk, tmp_path).structured("composer", SYSTEM, MESSAGES, Answer)
    assert "temperature" not in sdk.calls[0]


# --- response parsing and accounting -----------------------------------------------------


def test_live_returns_parsed_schema_instance(tmp_path: Path) -> None:
    """The text block's JSON is validated into the requested pydantic type."""
    obj, _ = _live(FakeSDK(), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert isinstance(obj, Answer)
    assert obj == GOOD


@pytest.mark.parametrize("mode", [LLMMode.live, LLMMode.record])
def test_usage_cost_equals_prices_cost_usd(mode: LLMMode, tmp_path: Path) -> None:
    """Accounting: Usage maps the SDK's four token counts and prices them via prices.cost_usd.

    Note the SDK's input_tokens excludes cached tokens; cache_creation/cache_read are separate.
    """
    sdk = FakeSDK(
        [
            make_response(
                input_tokens=1_234,
                output_tokens=567,
                cache_creation_input_tokens=8_000,
                cache_read_input_tokens=42_000,
            )
        ]
    )
    _, usage = _live(sdk, tmp_path, mode).structured("composer", SYSTEM, MESSAGES, Answer)
    assert (usage.input_tokens, usage.cache_write_tokens) == (1_234, 8_000)
    assert (usage.cache_read_tokens, usage.output_tokens) == (42_000, 567)
    assert usage.model == "claude-sonnet-5-5"
    assert usage.batch is False
    assert usage.cost_usd == cost_usd(
        "claude-sonnet-5-5",
        input_tokens=1_234,
        cache_write_tokens=8_000,
        cache_read_tokens=42_000,
        output_tokens=567,
    )
    assert usage.cost_usd > 0
    assert isinstance(usage.latency_ms, int)
    assert usage.latency_ms >= 0


def test_usage_with_missing_cache_counts_treated_as_zero(tmp_path: Path) -> None:
    """The SDK reports cache counters as None when caching was not used; that must mean zero."""
    resp = make_response(input_tokens=100, output_tokens=10)
    resp.usage.cache_creation_input_tokens = None
    resp.usage.cache_read_input_tokens = None
    _, usage = _live(FakeSDK([resp]), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert (usage.cache_write_tokens, usage.cache_read_tokens) == (0, 0)
    assert usage.cost_usd == cost_usd(
        "claude-haiku-5-5",
        input_tokens=100,
        cache_write_tokens=0,
        cache_read_tokens=0,
        output_tokens=10,
    )


def test_live_mode_writes_no_cassette(tmp_path: Path) -> None:
    """Only record mode persists responses; live leaves the cassette dir untouched."""
    _live(FakeSDK(), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert list(tmp_path.glob("*.json")) == []


def test_truncated_response_raises_instead_of_returning_partial(tmp_path: Path) -> None:
    """stop_reason=max_tokens means the JSON may be cut off: fail, never return a guess."""
    cut = make_response('{"value": 4', stop_reason="max_tokens")
    with pytest.raises((ValueError, RuntimeError)):
        _live(FakeSDK([cut]), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)


def test_schema_invalid_json_raises(tmp_path: Path) -> None:
    """Valid JSON of the wrong shape is rejected by the schema (pydantic ValidationError)."""
    bad = make_response('{"value": "not-an-int"}')
    with pytest.raises((ValueError, RuntimeError)):
        _live(FakeSDK([bad]), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)


# --- request_key -------------------------------------------------------------------------

_M = "claude-haiku-5-5"


def _key(client: LLMClient, **over) -> str:  # type: ignore[no-untyped-def]
    args = {
        "role_or_model": _M,
        "system": SYSTEM,
        "messages": MESSAGES,
        "schema": Answer,
        "max_tokens": 2048,
    }
    args.update(over)
    return client.request_key(
        args["role_or_model"],
        args["system"],
        args["messages"],
        args["schema"],
        max_tokens=args["max_tokens"],
    )


def test_request_key_is_sha256_hex_and_stable() -> None:
    """Key is 64 lowercase hex chars and identical across calls and client instances."""
    key = _key(LLMClient(mode=LLMMode.stub))
    assert len(key) == 64
    assert key == key.lower()
    int(key, 16)
    assert key == _key(LLMClient(mode=LLMMode.stub))


def test_request_key_ignores_message_dict_key_order() -> None:
    """Canonical JSON sorts keys, so {'role','content'} order must not change the hash."""
    c = LLMClient(mode=LLMMode.stub)
    a = [{"role": "user", "content": "hi"}]
    b = [{"content": "hi", "role": "user"}]
    assert _key(c, messages=a) == _key(c, messages=b)


@pytest.mark.parametrize(
    "change",
    [
        {"role_or_model": "claude-sonnet-5-5"},
        {"system": SYSTEM + " "},
        {"messages": [{"role": "user", "content": "What was FY2022 revenue?"}]},
        {"schema": DefaultedAnswer},
        {"max_tokens": 4096},
    ],
    ids=["model", "system", "messages", "schema", "max_tokens"],
)
def test_request_key_changes_with_every_input(change: dict) -> None:  # type: ignore[type-arg]
    """Each hashed component must matter, or replay would serve a stale cassette."""
    c = LLMClient(mode=LLMMode.stub)
    assert _key(c, **change) != _key(c)


def test_request_key_resolves_roles_to_model_ids() -> None:
    """Role 'planner' and its concrete model hash identically (cassettes survive role renames)."""
    c = LLMClient(mode=LLMMode.stub)
    assert _key(c, role_or_model="planner") == _key(c, role_or_model="claude-haiku-5-5")
    assert _key(c, role_or_model="planner") != _key(c, role_or_model="judge")


def test_request_key_depends_on_schema_content_not_just_name() -> None:
    """Two schemas with the same class name but different fields must not collide."""

    class Thing(DefaultedAnswer):
        extra: int = 0

    class Thing2(DefaultedAnswer):
        other: str = ""

    Thing2.__name__ = "Thing"
    c = LLMClient(mode=LLMMode.stub)
    assert _key(c, schema=Thing) != _key(c, schema=Thing2)
