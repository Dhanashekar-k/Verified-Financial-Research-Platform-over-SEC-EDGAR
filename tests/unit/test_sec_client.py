"""Unit tests for ``pipelines/fetch/sec_client.py`` (M01 section 3.1, contracts section 1.7).

Everything here runs against ``respx`` mocks and a fake clock, so no test touches the network
(CLAUDE.md rule 4) and none really sleeps. SEC access rules under test (CLAUDE.md rule 9):
User-Agent on every request, at most 8 req/s through one shared token bucket, retries with
exponential backoff and full jitter, Retry-After honored, at most 6 attempts.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pipelines.fetch import sec_client as sec_client_mod
from pipelines.fetch.sec_client import (
    ChecksumMismatchError,
    SecClient,
    SecClientConfigError,
    SecRequestError,
    TokenBucket,
)

UA = "Acme Research research-team@acme-research.org"
URL = "https://data.sec.gov/submissions/CIK0000320193.json"
EPS = 1e-6


# --------------------------------------------------------------------------------------
# Test doubles: a fake clock, a counting bucket, deterministic RNGs
# --------------------------------------------------------------------------------------


class FakeTime:
    """Fake monotonic clock. Sleeping advances the clock and records the request.

    Two sleep callables share one clock so bucket waits and backoff waits are recorded
    separately but still move time forward together.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.start = start
        self.bucket_sleeps: list[float] = []
        self.backoff_sleeps: list[float] = []
        self._calls = 0

    def clock(self) -> float:
        return self.now

    def _advance(self, seconds: float) -> None:
        assert seconds >= 0, f"negative sleep requested: {seconds}"
        self._calls += 1
        # Guard against an implementation that busy-loops on sleep(0) forever.
        assert self._calls < 100_000, "runaway sleep loop"
        self.now += seconds

    def bucket_sleep(self, seconds: float) -> None:
        self.bucket_sleeps.append(seconds)
        self._advance(seconds)

    def backoff_sleep(self, seconds: float) -> None:
        self.backoff_sleeps.append(seconds)
        self._advance(seconds)

    @property
    def elapsed(self) -> float:
        return self.now - self.start


class CountingBucket(TokenBucket):
    """A real TokenBucket that also counts acquire() calls (spy on a collaborator)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.acquire_count = 0

    def acquire(self) -> None:
        self.acquire_count += 1
        super().acquire()


class ConstRng(random.Random):
    """RNG whose random() is constant, so jitter is pinned to an end of its range.

    ``uniform(a, b)`` in the stdlib is ``a + (b - a) * random()``, so this pins both
    ``rng.random() * cap`` and ``rng.uniform(0, cap)`` style full jitter.
    """

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


NEAR_ONE = 1.0 - 2**-40  # the top of the jitter range, strictly below 1.0


def make_bucket(fake: FakeTime, rate: float = 8.0, capacity: float = 1.0) -> CountingBucket:
    return CountingBucket(rate=rate, capacity=capacity, clock=fake.clock, sleep=fake.bucket_sleep)


def make_client(fake: FakeTime, bucket: TokenBucket | None = None, **kwargs: Any) -> SecClient:
    kwargs.setdefault("user_agent", UA)
    kwargs.setdefault("rng", random.Random(1234))
    return SecClient(
        bucket=bucket if bucket is not None else make_bucket(fake),
        sleep=fake.backoff_sleep,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read a developer's real .env or SEC_USER_AGENT; run from an empty directory."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


def _ok(body: bytes = b"ok") -> httpx.Response:
    return httpx.Response(200, content=body)


# --------------------------------------------------------------------------------------
# 1. Rate limiting
# --------------------------------------------------------------------------------------


def test_thirty_gets_are_paced_at_eight_per_second(fake: FakeTime, respx_mock: respx.MockRouter):
    """30 calls at 8 req/s need >= 29/8 s of waiting, and no 1 s window holds more than 8.

    This is the SEC fair-access limit (<= 10 req/s); we run at 8 for headroom.
    """
    stamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(fake.now)
        return _ok(b"{}")

    respx_mock.get(URL).mock(side_effect=handler)
    client = make_client(fake, bucket=make_bucket(fake, rate=8.0, capacity=1.0), rate=8.0)

    for _ in range(30):
        client.get(URL)

    assert len(stamps) == 30
    assert sum(fake.bucket_sleeps) >= 29 / 8 - EPS
    for t0 in stamps:
        in_window = [t for t in stamps if t0 <= t < t0 + 1.0 - 1e-9]
        assert len(in_window) <= 8, f"{len(in_window)} requests inside 1 s starting at {t0}"


def test_default_bucket_is_shared_between_clients(respx_mock: respx.MockRouter):
    """Two clients built without ``bucket=`` must draw from one process-wide bucket.

    Per-client buckets would let N clients exceed the SEC limit N-fold. The bucket is located
    by type in the instance attributes, because the interface does not name the attribute.
    """
    respx_mock.get(URL).mock(return_value=_ok())
    sleeps: list[float] = []
    c1 = SecClient(user_agent=UA, sleep=sleeps.append, rng=random.Random(1))
    c2 = SecClient(user_agent=UA, sleep=sleeps.append, rng=random.Random(2))
    # One request each, in case the shared bucket is created lazily on first use.
    c1.get(URL)
    c2.get(URL)

    def bucket_of(client: SecClient) -> TokenBucket:
        found = [v for v in vars(client).values() if isinstance(v, TokenBucket)]
        assert len(found) == 1, "expected the client to hold exactly one TokenBucket attribute"
        return found[0]

    assert bucket_of(c1) is bucket_of(c2)


def test_explicit_bucket_is_used_instead_of_shared(fake: FakeTime, respx_mock: respx.MockRouter):
    """A bucket passed in is the one that gets acquired from (so tests and callers can inject)."""
    respx_mock.get(URL).mock(return_value=_ok())
    mine = make_bucket(fake)
    client = make_client(fake, bucket=mine)

    client.get(URL)

    assert mine.acquire_count == 1


def test_bucket_first_acquire_with_full_bucket_does_not_sleep(fake: FakeTime):
    """A fresh bucket starts full: the first token is free (the 29/8 bound relies on this)."""
    bucket = make_bucket(fake, rate=8.0, capacity=1.0)

    bucket.acquire()

    assert fake.bucket_sleeps == []
    assert fake.elapsed == 0


def test_bucket_capacity_allows_a_burst_then_throttles(fake: FakeTime):
    """capacity=3 allows 3 immediate acquires; the 4th waits one token interval (1/rate)."""
    bucket = make_bucket(fake, rate=4.0, capacity=3.0)

    for _ in range(3):
        bucket.acquire()
    assert fake.elapsed == pytest.approx(0, abs=EPS)

    bucket.acquire()
    assert fake.elapsed == pytest.approx(1 / 4.0, abs=EPS)


def test_bucket_does_not_accumulate_beyond_capacity_while_idle(fake: FakeTime):
    """After a long idle period only ``capacity`` tokens exist, not rate * idle_time.

    Otherwise a quiet hour would permit a huge burst that violates the SEC limit.
    """
    bucket = make_bucket(fake, rate=4.0, capacity=2.0)
    bucket.acquire()
    bucket.acquire()
    fake.now += 1000.0  # idle
    before = fake.now

    for _ in range(3):
        bucket.acquire()

    # 2 tokens available instantly, the third needs 1/4 s.
    assert fake.now - before >= 1 / 4.0 - EPS


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rate=st.floats(min_value=1.0, max_value=50.0),
    n=st.integers(min_value=2, max_value=100),
    capacity=st.floats(min_value=1.0, max_value=5.0),
)
def test_property_bucket_total_time_lower_bound(rate: float, n: int, capacity: float):
    """For any rate in [1, 50] and n in [2, 100], n acquires take >= (n - capacity) / rate.

    The bucket may release ``capacity`` tokens at once; everything after is paced by rate.
    """
    fake = FakeTime()
    bucket = TokenBucket(rate=rate, capacity=capacity, clock=fake.clock, sleep=fake.bucket_sleep)

    for _ in range(n):
        bucket.acquire()

    assert fake.elapsed >= (n - capacity) / rate - EPS


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rate=st.floats(min_value=1.0, max_value=50.0),
    n=st.integers(min_value=2, max_value=100),
    capacity=st.floats(min_value=1.0, max_value=5.0),
)
def test_property_bucket_never_exceeds_rate_in_any_window(rate: float, n: int, capacity: float):
    """In any 1 s window the bucket grants at most ``capacity + rate`` tokens.

    This is the upper-bound counterpart of the lower-bound test: the bucket must not hand
    out tokens faster than the configured rate once the burst allowance is spent.
    """
    fake = FakeTime()
    bucket = TokenBucket(rate=rate, capacity=capacity, clock=fake.clock, sleep=fake.bucket_sleep)
    stamps: list[float] = []

    for _ in range(n):
        bucket.acquire()
        stamps.append(fake.now)

    for t0 in stamps:
        in_window = [t for t in stamps if t0 <= t < t0 + 1.0]
        assert len(in_window) <= capacity + rate + EPS


# --------------------------------------------------------------------------------------
# 2. User-Agent and configuration
# --------------------------------------------------------------------------------------


def _assert_headers(calls: Any) -> None:
    assert len(calls) > 0
    for call in calls:
        headers = call.request.headers
        assert headers["User-Agent"] == UA
        encoding = headers["Accept-Encoding"].lower()
        assert "gzip" in encoding
        assert "deflate" in encoding


def test_get_sends_user_agent_and_accept_encoding(fake: FakeTime, respx_mock: respx.MockRouter):
    """``get`` carries the configured User-Agent and Accept-Encoding: gzip, deflate (SEC rules)."""
    route = respx_mock.get(URL).mock(return_value=_ok())

    make_client(fake).get(URL)

    _assert_headers(route.calls)


def test_get_json_sends_user_agent_and_accept_encoding(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """``get_json`` must not bypass the header policy."""
    route = respx_mock.get(URL).mock(return_value=_ok(b'{"a": 1}'))

    make_client(fake).get_json(URL)

    _assert_headers(route.calls)


def test_download_sends_user_agent_and_accept_encoding(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """``download`` must not bypass the header policy (it may stream, unlike ``get``)."""
    route = respx_mock.get(URL).mock(return_value=_ok(b"data"))

    make_client(fake).download(URL, tmp_path / "out.bin")

    _assert_headers(route.calls)


def test_user_agent_is_sent_on_every_retried_attempt(fake: FakeTime, respx_mock: respx.MockRouter):
    """Retries are separate HTTP requests; SEC blocks requests without a User-Agent."""
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(429), _ok()]
    )

    make_client(fake).get(URL)

    assert route.call_count == 4
    _assert_headers(route.calls)


def test_user_agent_is_read_from_environment(
    fake: FakeTime, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
):
    """With no argument, the User-Agent comes from SEC_USER_AGENT (never hard-coded)."""
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    route = respx_mock.get(URL).mock(return_value=_ok())

    client = SecClient(bucket=make_bucket(fake), sleep=fake.backoff_sleep)
    client.get(URL)

    _assert_headers(route.calls)


def test_explicit_user_agent_overrides_environment(
    fake: FakeTime, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
):
    """An explicit argument wins over the environment variable."""
    monkeypatch.setenv("SEC_USER_AGENT", "Env Person env@envcorp.org")
    route = respx_mock.get(URL).mock(return_value=_ok())

    make_client(fake, user_agent=UA).get(URL)

    _assert_headers(route.calls)


def test_missing_env_and_no_argument_raises_config_error():
    """No User-Agent anywhere -> fail at construction, before any request can be made."""
    with pytest.raises(SecClientConfigError):
        SecClient()


@pytest.mark.parametrize("blank", ["", " ", "   \t "])
def test_blank_env_raises_config_error(monkeypatch: pytest.MonkeyPatch, blank: str):
    """A blank SEC_USER_AGENT is as bad as a missing one."""
    monkeypatch.setenv("SEC_USER_AGENT", blank)

    with pytest.raises(SecClientConfigError):
        SecClient()


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_argument_raises_config_error(blank: str):
    """A blank explicit User-Agent is rejected rather than silently falling back or sent."""
    with pytest.raises(SecClientConfigError):
        SecClient(user_agent=blank)


def test_placeholder_user_agent_raises_config_error(monkeypatch: pytest.MonkeyPatch):
    """The documented placeholder (``<your-email>`` from .env.example) must not be sent to SEC."""
    placeholder = "Jane Doe <your-email>"
    monkeypatch.setenv("SEC_USER_AGENT", placeholder)
    with pytest.raises(SecClientConfigError):
        SecClient()
    with pytest.raises(SecClientConfigError):
        SecClient(user_agent=placeholder)


# --------------------------------------------------------------------------------------
# 3. Retries and backoff
# --------------------------------------------------------------------------------------


def test_429_with_retry_after_is_honored(fake: FakeTime, respx_mock: respx.MockRouter):
    """429 + ``Retry-After: 2`` then 200: body returned, and we waited at least 2 s."""
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), _ok(b"done")]
    )

    body = make_client(fake).get(URL)

    assert body == b"done"
    assert route.call_count == 2
    assert sum(fake.backoff_sleeps) >= 2


def test_retry_after_is_a_floor_even_when_jitter_is_zero(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """Retry-After must not be scaled down by jitter: with rng == 0 we still wait >= 7 s."""
    respx_mock.get(URL).mock(side_effect=[httpx.Response(503, headers={"Retry-After": "7"}), _ok()])

    make_client(fake, rng=ConstRng(0.0)).get(URL)

    assert sum(fake.backoff_sleeps) >= 7


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_then_success(fake: FakeTime, respx_mock: respx.MockRouter, status: int):
    """429 and 5xx are transient: one failure followed by 200 succeeds on the second try."""
    route = respx_mock.get(URL).mock(side_effect=[httpx.Response(status), _ok(b"fine")])

    body = make_client(fake).get(URL)

    assert body == b"fine"
    assert route.call_count == 2


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow")])
def test_connection_error_then_success(
    fake: FakeTime, respx_mock: respx.MockRouter, exc: httpx.TransportError
):
    """Connection-level failures are retried like 5xx."""
    route = respx_mock.get(URL).mock(side_effect=[exc, _ok(b"back")])

    body = make_client(fake).get(URL)

    assert body == b"back"
    assert route.call_count == 2


def test_six_consecutive_500s_raise_after_exactly_six_requests(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """Default max_attempts=6: exactly 6 requests are made (no 7th), then SecRequestError."""
    route = respx_mock.get(URL).mock(return_value=httpx.Response(500))

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert route.call_count == 6
    assert info.value.attempts == 6
    assert info.value.status_code == 500
    assert str(info.value.url) == URL


def test_no_sleep_after_the_final_failed_attempt(fake: FakeTime, respx_mock: respx.MockRouter):
    """6 attempts means 5 waits: sleeping after the last failure only delays the error."""
    respx_mock.get(URL).mock(return_value=httpx.Response(500))

    with pytest.raises(SecRequestError):
        make_client(fake).get(URL)

    assert len(fake.backoff_sleeps) == 5


def test_max_attempts_is_configurable(fake: FakeTime, respx_mock: respx.MockRouter):
    """``max_attempts=3`` stops after exactly 3 requests."""
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SecRequestError) as info:
        make_client(fake, max_attempts=3).get(URL)

    assert route.call_count == 3
    assert info.value.attempts == 3


def test_exhausted_connection_errors_have_no_status_code(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """If every attempt fails before a response, ``status_code`` is None and attempts == 6."""
    route = respx_mock.get(URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert route.call_count == 6
    assert info.value.status_code is None
    assert info.value.attempts == 6


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_errors_are_not_retried(fake: FakeTime, respx_mock: respx.MockRouter, status: int):
    """4xx other than 429 will not change on retry: 1 request, SecRequestError, no waiting."""
    route = respx_mock.get(URL).mock(return_value=httpx.Response(status))

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert route.call_count == 1
    assert info.value.status_code == status
    assert info.value.attempts == 1
    assert str(info.value.url) == URL
    assert fake.backoff_sleeps == []


def _backoff_sleeps_for_six_500s(rng: random.Random) -> list[float]:
    """Run the client against permanent 500s and return the waits it requested."""
    fake = FakeTime()
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SecRequestError):
            make_client(fake, rng=rng).get(URL)
    return fake.backoff_sleeps


def test_backoff_cap_grows_exponentially():
    """With the jitter pinned to its maximum, the waits are the caps: base * 2**attempt.

    The base is not part of the interface, so the test derives it from the first wait and
    checks the doubling for the first four retries (later ones may hit a configured maximum).
    """
    caps = [s / NEAR_ONE for s in _backoff_sleeps_for_six_500s(ConstRng(NEAR_ONE))]

    assert len(caps) == 5
    assert caps[0] > 0
    for k in range(4):
        assert caps[k] == pytest.approx(caps[0] * 2**k, rel=1e-6)
    assert caps == sorted(caps)


def test_backoff_uses_full_jitter_lower_bound_of_zero():
    """Full jitter draws from [0, cap]: with rng pinned to 0 every wait is 0 (not cap / 2)."""
    sleeps = _backoff_sleeps_for_six_500s(ConstRng(0.0))

    assert len(sleeps) == 5
    assert all(s == pytest.approx(0, abs=1e-9) for s in sleeps)


@pytest.mark.parametrize("seed", [0, 1, 42, 2024])
def test_seeded_jitter_stays_within_cap_for_each_attempt(seed: int):
    """Each wait is within [0, base * 2**attempt]; asserted on bounds, not exact values."""
    caps = [s / NEAR_ONE for s in _backoff_sleeps_for_six_500s(ConstRng(NEAR_ONE))]

    sleeps = _backoff_sleeps_for_six_500s(random.Random(seed))

    assert len(sleeps) == len(caps) == 5
    for attempt, (sleep, cap) in enumerate(zip(sleeps, caps, strict=True)):
        assert 0 <= sleep <= cap * (1 + 1e-9), f"attempt {attempt}: {sleep} not in [0, {cap}]"


def test_jitter_actually_varies_with_the_rng():
    """Different seeds give different waits; a fixed (jitter-free) backoff would not."""
    a = _backoff_sleeps_for_six_500s(random.Random(1))
    b = _backoff_sleeps_for_six_500s(random.Random(2))

    assert a != b


def test_same_seed_gives_same_waits():
    """All randomness comes from the injected rng, so a seed makes the schedule reproducible."""
    assert _backoff_sleeps_for_six_500s(random.Random(7)) == _backoff_sleeps_for_six_500s(
        random.Random(7)
    )


def test_every_retry_attempt_acquires_a_bucket_token(fake: FakeTime, respx_mock: respx.MockRouter):
    """Retries are requests too: 429, 503, 200 means three acquires, so retries can't burst."""
    route = respx_mock.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(503), _ok()])
    bucket = make_bucket(fake)

    make_client(fake, bucket=bucket).get(URL)

    assert route.call_count == 3
    assert bucket.acquire_count == 3


def test_connection_error_retry_also_acquires_a_token(fake: FakeTime, respx_mock: respx.MockRouter):
    """A failed connect still counts against SEC's limit, so the retry takes a new token."""
    route = respx_mock.get(URL).mock(side_effect=[httpx.ConnectError("x"), _ok()])
    bucket = make_bucket(fake)

    make_client(fake, bucket=bucket).get(URL)

    assert bucket.acquire_count == route.call_count == 2


def test_retries_are_paced_by_the_bucket_even_with_zero_backoff(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """With jitter 0 the bucket alone must still space six attempts at >= 1/8 s each."""
    stamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(fake.now)
        return httpx.Response(500)

    respx_mock.get(URL).mock(side_effect=handler)

    with pytest.raises(SecRequestError):
        make_client(fake, rng=ConstRng(0.0)).get(URL)

    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert len(stamps) == 6
    assert all(g >= 1 / 8 - EPS for g in gaps)


def test_gzip_encoded_response_is_decoded(fake: FakeTime, respx_mock: respx.MockRouter):
    """We advertise gzip, so a gzip body must come back decoded to the original bytes."""
    respx_mock.get(URL).mock(
        return_value=httpx.Response(
            200, content=gzip.compress(b"hello"), headers={"Content-Encoding": "gzip"}
        )
    )

    assert make_client(fake).get(URL) == b"hello"


# --------------------------------------------------------------------------------------
# 4. get_json
# --------------------------------------------------------------------------------------


def test_get_json_parses_object(fake: FakeTime, respx_mock: respx.MockRouter):
    """``get_json`` returns the parsed JSON document."""
    payload = {"cik": 320193, "name": "Apple Inc.", "facts": {"a": [1, 2, 3]}}
    respx_mock.get(URL).mock(return_value=_ok(json.dumps(payload).encode()))

    assert make_client(fake).get_json(URL) == payload


@pytest.mark.parametrize("body", [b"", b"<html>Rate limit</html>", b'{"a": '])
def test_get_json_invalid_json_raises_value_error(
    fake: FakeTime, respx_mock: respx.MockRouter, body: bytes
):
    """Garbage bodies (e.g. an HTML error page served with 200) must fail loudly, not return."""
    respx_mock.get(URL).mock(return_value=_ok(body))

    with pytest.raises(ValueError):
        make_client(fake).get_json(URL)


def test_get_json_propagates_request_errors(fake: FakeTime, respx_mock: respx.MockRouter):
    """A 404 surfaces as SecRequestError, not as a JSON error."""
    respx_mock.get(URL).mock(return_value=httpx.Response(404, content=b"not found"))

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get_json(URL)

    assert info.value.status_code == 404


# --------------------------------------------------------------------------------------
# 5. download
# --------------------------------------------------------------------------------------

BODY = bytes(range(256)) * 4096  # 1 MiB, non-text, multiple chunks


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_writes_file_and_returns_sha256(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """The file holds the response bytes and the return value is their lowercase hex sha256."""
    respx_mock.get(URL).mock(return_value=_ok(BODY))
    dest = tmp_path / "out.bin"

    digest = make_client(fake).download(URL, dest)

    assert dest.read_bytes() == BODY
    assert digest == _sha(BODY)
    assert len(digest) == 64


def test_download_empty_body(fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path):
    """An empty 200 is a valid (empty) file with the well-known empty-string digest."""
    respx_mock.get(URL).mock(return_value=_ok(b""))
    dest = tmp_path / "empty.bin"

    digest = make_client(fake).download(URL, dest)

    assert dest.read_bytes() == b""
    assert digest == _sha(b"")


def test_download_with_matching_checksum_succeeds(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A correct expected sha256 passes and the file is kept."""
    respx_mock.get(URL).mock(return_value=_ok(BODY))
    dest = tmp_path / "out.bin"

    digest = make_client(fake).download(URL, dest, sha256=_sha(BODY))

    assert digest == _sha(BODY)
    assert dest.read_bytes() == BODY


def test_download_wrong_checksum_raises_and_leaves_no_file(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A bad checksum raises ChecksumMismatchError; nothing is left at dest or beside it."""
    respx_mock.get(URL).mock(return_value=_ok(BODY))
    dest = tmp_path / "out.bin"

    with pytest.raises(ChecksumMismatchError):
        make_client(fake).download(URL, dest, sha256="0" * 64)

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_wrong_checksum_leaves_existing_dest_untouched(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A previously good file must survive a corrupt re-download (idempotent reruns)."""
    respx_mock.get(URL).mock(return_value=_ok(BODY))
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"previous good content")

    with pytest.raises(ChecksumMismatchError):
        make_client(fake).download(URL, dest, sha256="f" * 64)

    assert dest.read_bytes() == b"previous good content"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


def test_download_creates_parent_directories(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """Missing parent directories (e.g. data/raw/fsds/2023q3/) are created."""
    respx_mock.get(URL).mock(return_value=_ok(b"zip-bytes"))
    dest = tmp_path / "data" / "raw" / "fsds" / "2023q3.zip"

    make_client(fake).download(URL, dest)

    assert dest.read_bytes() == b"zip-bytes"


def test_download_retries_429_then_succeeds(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A 429 + Retry-After during download is retried and the final file is complete."""
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), _ok(BODY)]
    )
    dest = tmp_path / "out.bin"

    digest = make_client(fake).download(URL, dest)

    assert route.call_count == 2
    assert sum(fake.backoff_sleeps) >= 1
    assert dest.read_bytes() == BODY
    assert digest == _sha(BODY)


def test_download_404_raises_and_creates_nothing(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A non-retryable status leaves no file and no temp file behind."""
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    dest = tmp_path / "out.bin"

    with pytest.raises(SecRequestError) as info:
        make_client(fake).download(URL, dest)

    assert info.value.status_code == 404
    assert list(tmp_path.iterdir()) == []


class _Chunks(httpx.SyncByteStream):
    """Response body delivered in chunks; calls ``on_chunk`` before each, optionally fails."""

    def __init__(
        self, chunks: list[bytes], on_chunk: Callable[[], None], fail_after: int | None = None
    ) -> None:
        self.chunks = chunks
        self.on_chunk = on_chunk
        self.fail_after = fail_after

    def __iter__(self):
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise httpx.ReadError("connection reset mid-body")
            self.on_chunk()
            yield chunk


def test_download_failure_midway_leaves_no_partial_file(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """If the transfer dies mid-body, dest never appears and no temp file is left over.

    The stream fails on every attempt, so the client exhausts its retries (or gives up on the
    first error); either way the download must not succeed and must not leave debris.
    """
    chunks = [b"a" * 1000, b"b" * 1000, b"c" * 1000]
    dest = tmp_path / "out.bin"
    seen_dest: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = _Chunks(chunks, lambda: seen_dest.append(dest.exists()), fail_after=2)
        return httpx.Response(200, stream=stream)

    respx_mock.get(URL).mock(side_effect=handler)

    with pytest.raises((SecRequestError, httpx.HTTPError)):
        make_client(fake).download(URL, dest)

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []
    assert not any(seen_dest), "dest appeared while the transfer was still in progress"


def test_download_failure_midway_keeps_existing_dest(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A failed refresh must not truncate or corrupt a previously downloaded file."""
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")

    def handler(request: httpx.Request) -> httpx.Response:
        stream = _Chunks([b"new1", b"new2"], lambda: None, fail_after=1)
        return httpx.Response(200, stream=stream)

    respx_mock.get(URL).mock(side_effect=handler)

    with pytest.raises((SecRequestError, httpx.HTTPError)):
        make_client(fake).download(URL, dest)

    assert dest.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


def test_download_is_atomic_while_in_flight(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """During a successful transfer dest keeps its old content; it flips only at the end."""
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"OLD")
    snapshots: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = _Chunks(
            [b"NEW-1", b"NEW-2", b"NEW-3"], lambda: snapshots.append(dest.read_bytes())
        )
        return httpx.Response(200, stream=stream)

    respx_mock.get(URL).mock(side_effect=handler)

    make_client(fake).download(URL, dest)

    assert snapshots, "response body was never read"
    assert all(s == b"OLD" for s in snapshots)
    assert dest.read_bytes() == b"NEW-1NEW-2NEW-3"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


def test_download_renames_a_temp_file_from_the_same_directory(
    fake: FakeTime,
    respx_mock: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The final step is a rename from a temp file in dest's own directory.

    Same directory matters because rename is only atomic within one filesystem.
    """
    respx_mock.get(URL).mock(return_value=_ok(b"payload"))
    dest = tmp_path / "sub" / "out.bin"
    renames: list[tuple[Path, Path]] = []
    real_replace, real_rename = os.replace, os.rename

    def spy_replace(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst, *a, **kw)

    def spy_rename(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "rename", spy_rename)

    make_client(fake).download(URL, dest)

    final = [(s, d) for s, d in renames if d == dest]
    assert len(final) == 1, f"expected one rename onto dest, saw {renames}"
    src, _ = final[0]
    assert src != dest
    assert src.parent == dest.parent
    assert not src.exists()


def test_download_success_leaves_no_temp_files(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """After a successful download the directory contains only the destination file."""
    respx_mock.get(URL).mock(side_effect=[httpx.Response(503), _ok(BODY)])
    dest = tmp_path / "out.bin"

    make_client(fake).download(URL, dest)

    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


# --------------------------------------------------------------------------------------
# 6. Review hardening: redirects, rate ceiling, decoding errors, Retry-After, threads
# --------------------------------------------------------------------------------------

REDIRECT_TARGET = "https://www.sec.gov/Archives/edgar/data/320193/moved.htm"


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def test_redirect_hop_draws_its_own_bucket_token(fake: FakeTime, respx_mock: respx.MockRouter):
    """Each hop is a real request to sec.gov, so each hop must take a token (SEC <= 10 req/s)."""
    respx_mock.get(URL).mock(return_value=_redirect(REDIRECT_TARGET))
    target = respx_mock.get(REDIRECT_TARGET).mock(return_value=_ok(b"moved"))
    bucket = make_bucket(fake)

    body = make_client(fake, bucket=bucket).get(URL)

    assert body == b"moved"
    assert target.call_count == 1
    assert bucket.acquire_count == 2


def test_redirect_keeps_user_agent_on_sec_hosts(fake: FakeTime, respx_mock: respx.MockRouter):
    """The hop to another sec.gov host still carries the User-Agent."""
    respx_mock.get(URL).mock(return_value=_redirect(REDIRECT_TARGET, 301))
    target = respx_mock.get(REDIRECT_TARGET).mock(return_value=_ok())

    make_client(fake).get(URL)

    _assert_headers(target.calls)


def test_relative_redirect_is_followed(fake: FakeTime, respx_mock: respx.MockRouter):
    """A relative Location resolves against the current URL."""
    respx_mock.get(URL).mock(return_value=_redirect("/other.json"))
    target = respx_mock.get("https://data.sec.gov/other.json").mock(return_value=_ok(b"{}"))

    make_client(fake).get(URL)

    assert target.call_count == 1


def test_redirect_to_non_sec_host_is_refused(fake: FakeTime, respx_mock: respx.MockRouter):
    """Never forward our User-Agent (it contains a contact email) or spend a token off sec.gov."""
    respx_mock.get(URL).mock(return_value=_redirect("https://evil.example.com/steal"))
    evil = respx_mock.get("https://evil.example.com/steal").mock(return_value=_ok())

    with pytest.raises(SecRequestError):
        make_client(fake).get(URL)

    assert evil.call_count == 0


def test_redirect_to_plain_http_is_refused(fake: FakeTime, respx_mock: respx.MockRouter):
    """A downgrade to http:// is not followed, even on a sec.gov host."""
    http_url = "http://www.sec.gov/insecure"
    respx_mock.get(URL).mock(return_value=_redirect(http_url))
    insecure = respx_mock.get(http_url).mock(return_value=_ok())

    with pytest.raises(SecRequestError):
        make_client(fake).get(URL)

    assert insecure.call_count == 0


def test_redirect_loop_is_bounded(fake: FakeTime, respx_mock: respx.MockRouter):
    """A redirect loop ends in SecRequestError after a small, fixed number of requests."""
    route = respx_mock.get(URL).mock(return_value=_redirect(URL))

    with pytest.raises(SecRequestError):
        make_client(fake).get(URL)

    assert 1 < route.call_count <= 12


def test_redirect_without_location_is_an_error(fake: FakeTime, respx_mock: respx.MockRouter):
    respx_mock.get(URL).mock(return_value=httpx.Response(302))

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert info.value.status_code == 302


def test_rate_above_sec_ceiling_is_rejected():
    """CLAUDE.md rule 9: never above 8 req/s, whatever the caller asks for."""
    with pytest.raises(ValueError):
        SecClient(user_agent=UA, rate=50.0)


def test_shared_bucket_rejects_a_conflicting_rate(monkeypatch: pytest.MonkeyPatch):
    """A later client must not silently run at the first client's rate (or vice versa)."""
    monkeypatch.setattr(sec_client_mod, "_shared_bucket", None)
    SecClient(user_agent=UA, rate=8.0)

    with pytest.raises(ValueError):
        SecClient(user_agent=UA, rate=2.0)
    SecClient(user_agent=UA, rate=8.0)  # the same rate is fine


def _corrupt_gzip(request: httpx.Request | None = None) -> httpx.Response:
    """A 200 whose body claims gzip but isn't. The stream keeps decoding lazy (client side)."""
    return httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=httpx.ByteStream(b"definitely not gzip"),
    )


def test_corrupt_gzip_body_is_retried(fake: FakeTime, respx_mock: respx.MockRouter):
    """A body that fails to decode is a transient transfer fault, not a crash."""
    route = respx_mock.get(URL).mock(side_effect=[_corrupt_gzip(), _ok(b"clean")])

    assert make_client(fake).get(URL) == b"clean"
    assert route.call_count == 2


def test_persistent_decoding_error_becomes_sec_request_error(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """Callers see one failure type, never a raw httpx exception."""
    respx_mock.get(URL).mock(side_effect=_corrupt_gzip)

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert info.value.attempts == 6
    assert info.value.status_code is None


def test_408_request_timeout_is_retried(fake: FakeTime, respx_mock: respx.MockRouter):
    route = respx_mock.get(URL).mock(side_effect=[httpx.Response(408), _ok(b"ok")])

    assert make_client(fake).get(URL) == b"ok"
    assert route.call_count == 2


def test_retry_after_beyond_the_cap_fails_instead_of_retrying_early(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """If the server says wait 900 s we must not come back after 300 s: give up, say why."""
    route = respx_mock.get(URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "900"})
    )

    with pytest.raises(SecRequestError) as info:
        make_client(fake).get(URL)

    assert route.call_count == 1
    assert info.value.status_code == 429
    assert fake.backoff_sleeps == []


def test_retry_after_at_the_cap_is_honored(fake: FakeTime, respx_mock: respx.MockRouter):
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": str(sec_client_mod.MAX_RETRY_AFTER_S)}),
            _ok(),
        ]
    )

    make_client(fake, rng=ConstRng(0.0)).get(URL)

    assert sum(fake.backoff_sleeps) >= sec_client_mod.MAX_RETRY_AFTER_S


def test_retry_after_http_date_is_honored(fake: FakeTime, respx_mock: respx.MockRouter):
    """Retry-After may be an HTTP-date; we wait until then (within a generous tolerance)."""
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=40), usegmt=True)
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": when}), _ok()]
    )

    make_client(fake, rng=ConstRng(0.0)).get(URL)

    assert 30 <= sum(fake.backoff_sleeps) <= 41


def test_garbage_retry_after_is_ignored(fake: FakeTime, respx_mock: respx.MockRouter):
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "soon"}), _ok()]
    )

    assert make_client(fake, rng=ConstRng(0.0)).get(URL) == b"ok"


def test_backoff_never_exceeds_the_ceiling(fake: FakeTime, respx_mock: respx.MockRouter):
    """With many attempts the exponential cap flattens at BACKOFF_MAX_S."""
    respx_mock.get(URL).mock(return_value=httpx.Response(500))

    with pytest.raises(SecRequestError):
        make_client(fake, max_attempts=14, rng=ConstRng(NEAR_ONE)).get(URL)

    assert len(fake.backoff_sleeps) == 13
    assert max(fake.backoff_sleeps) <= sec_client_mod.BACKOFF_MAX_S
    assert fake.backoff_sleeps[-1] == pytest.approx(sec_client_mod.BACKOFF_MAX_S, rel=1e-6)


def test_bucket_holds_the_rate_across_threads():
    """4 threads x 20 acquires on one bucket behave like 80 sequential acquires."""
    fake = FakeTime()
    bucket = TokenBucket(rate=8.0, capacity=1.0, clock=fake.clock, sleep=fake.bucket_sleep)

    def worker() -> None:
        for _ in range(20):
            bucket.acquire()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads)
    assert fake.elapsed >= (80 - 1) / 8.0 - EPS


def test_download_midway_failure_then_success_recovers(
    fake: FakeTime, respx_mock: respx.MockRouter, tmp_path: Path
):
    """A dropped connection mid-body is retried from a fresh temp file; dest ends up complete."""
    dest = tmp_path / "out.bin"
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                200, stream=_Chunks([b"part-1", b"part-2"], lambda: None, fail_after=1)
            )
        return httpx.Response(200, content=BODY)

    respx_mock.get(URL).mock(side_effect=handler)

    digest = make_client(fake).download(URL, dest)

    assert attempts["n"] == 2
    assert dest.read_bytes() == BODY
    assert digest == _sha(BODY)
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


# --------------------------------------------------------------------------------------
# 7. Second review: injected bucket ceiling, URL validation, redirect boundaries
# --------------------------------------------------------------------------------------


def test_injected_bucket_above_the_ceiling_is_rejected():
    """The 8 req/s cap must hold for callers that bring their own bucket too (rule 9)."""
    with pytest.raises(ValueError):
        SecClient(user_agent=UA, bucket=TokenBucket(rate=100.0))


@pytest.mark.parametrize(
    "bad_url",
    ["sec.gov/foo", "ftp://www.sec.gov/x", "http://www.sec.gov/x", "https://example.com/"],
)
def test_non_sec_or_malformed_urls_are_rejected_before_any_request(
    fake: FakeTime, respx_mock: respx.MockRouter, bad_url: str
):
    """A caller bug fails fast: no retries, no tokens spent, no User-Agent sent off sec.gov."""
    bucket = make_bucket(fake)

    with pytest.raises(ValueError):
        make_client(fake, bucket=bucket).get(bad_url)

    assert bucket.acquire_count == 0
    assert fake.backoff_sleeps == []


@pytest.mark.parametrize(
    ("evil", "evil_host"),
    [
        ("https://sec.gov.evil.com/x", "sec.gov.evil.com"),
        ("https://evilsec.gov/x", "evilsec.gov"),
        ("https://www.sec.gov@evil.com/x", "evil.com"),
        ("https://evil.com/?www.sec.gov", "evil.com"),
    ],
)
def test_lookalike_redirect_hosts_are_refused(
    fake: FakeTime, respx_mock: respx.MockRouter, evil: str, evil_host: str
):
    """Suffix/substring tricks must not pass the sec.gov host check."""
    respx_mock.get(URL).mock(return_value=_redirect(evil))
    other = respx_mock.route(host=evil_host).mock(return_value=_ok())

    with pytest.raises(SecRequestError):
        make_client(fake).get(URL)

    assert other.call_count == 0


def test_redirect_to_the_bare_sec_gov_host_is_followed(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    respx_mock.get(URL).mock(return_value=_redirect("https://sec.gov/x"))
    target = respx_mock.get("https://sec.gov/x").mock(return_value=_ok())

    make_client(fake).get(URL)

    assert target.call_count == 1


def test_five_redirects_then_success_is_followed(fake: FakeTime, respx_mock: respx.MockRouter):
    hops = [f"https://www.sec.gov/hop{i}" for i in range(1, 6)]
    respx_mock.get(URL).mock(return_value=_redirect(hops[0]))
    for here, nxt in zip(hops, [*hops[1:], None], strict=True):
        respx_mock.get(here).mock(return_value=_redirect(nxt) if nxt else _ok(b"end"))

    assert make_client(fake).get(URL) == b"end"


def test_six_redirects_raise_after_exactly_six_requests(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """MAX_REDIRECTS is exactly 5: the sixth redirect response is refused, not followed."""
    bucket = make_bucket(fake)
    hops = [f"https://www.sec.gov/hop{i}" for i in range(1, 7)]
    respx_mock.get(URL).mock(return_value=_redirect(hops[0], 301))
    for here, nxt in zip(hops, [*hops[1:], "https://www.sec.gov/never"], strict=True):
        respx_mock.get(here).mock(return_value=_redirect(nxt, 301))

    with pytest.raises(SecRequestError) as info:
        make_client(fake, bucket=bucket).get(URL)

    assert bucket.acquire_count == 6
    assert info.value.status_code == 301
    assert info.value.attempts == 1


def test_redirect_then_500_then_success_restarts_from_the_original_url(
    fake: FakeTime, respx_mock: respx.MockRouter
):
    """Hops are not attempts: after a 5xx on the target the retry begins at the first URL again."""
    origin = respx_mock.get(URL).mock(return_value=_redirect(REDIRECT_TARGET))
    target = respx_mock.get(REDIRECT_TARGET).mock(side_effect=[httpx.Response(500), _ok(b"ok")])

    assert make_client(fake).get(URL) == b"ok"
    assert origin.call_count == 2
    assert target.call_count == 2
