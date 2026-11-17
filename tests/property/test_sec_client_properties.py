"""Property tests (Hypothesis) for the SEC client's token bucket.

The example-based tests live in `tests/unit/test_sec_client.py`.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pipelines.fetch.sec_client import TokenBucket
from tests.fake_time import FakeTime

EPS = 1e-6


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
