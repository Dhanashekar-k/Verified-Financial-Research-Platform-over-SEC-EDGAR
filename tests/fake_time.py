"""A fake monotonic clock shared by the unit and property tests of the SEC client."""

from __future__ import annotations


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
