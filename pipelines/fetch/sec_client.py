"""Shared HTTP client for SEC endpoints (CLAUDE.md rule 9; M01 section 3.1).

Every request to sec.gov / data.sec.gov goes through :class:`SecClient`:

* a User-Agent from ``SEC_USER_AGENT`` on every request (the SEC blocks anonymous clients);
* one process-wide token bucket (default 8 req/s, the SEC limit is 10) that every attempt,
  including retries, must draw from;
* retries on 429, 5xx and connection errors with exponential backoff and full jitter,
  honoring ``Retry-After``, at most 6 attempts, then :class:`SecRequestError`;
* ``download`` streams to a temp file in the destination directory and renames it into
  place, so a failed or corrupt transfer never leaves a partial file at ``dest``.

Entry points load ``.env`` (this module never does), so the environment is the only input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx

log = logging.getLogger(__name__)

DEFAULT_RATE = 8.0  # req/s; SEC's fair-access limit is 10, leave headroom
MAX_RATE = 8.0  # CLAUDE.md rule 9: never configure above this
DEFAULT_MAX_ATTEMPTS = 6
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BACKOFF_BASE_S = 0.5  # cap before attempt k's retry is BACKOFF_BASE_S * 2**k ...
BACKOFF_MAX_S = 30.0  # ... up to this ceiling
MAX_RETRY_AFTER_S = 300.0  # a longer Retry-After makes the request fail rather than retry early
_PLACEHOLDERS = ("<your-email>", "your-email@example.com")  # from .env.example and the contracts

_RETRYABLE_STATUSES = frozenset({408, 429})  # plus every 5xx

T = TypeVar("T")


@dataclass(frozen=True)
class _Retry:
    """A transient failure of one attempt (status is None if there was no response)."""

    status: int | None
    reason: str
    retry_after: float | None


class SecClientConfigError(Exception):
    """``SEC_USER_AGENT`` is missing, blank or still the documented placeholder."""


class SecRequestError(Exception):
    """A request failed for good: a non-retryable status, or retries were exhausted.

    ``status_code`` is the last HTTP status seen, or ``None`` if the last attempt failed
    before a response (connection error, mid-body failure).
    """

    def __init__(self, url: str, status_code: int | None, attempts: int, message: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.attempts = attempts
        detail = f"HTTP {status_code}" if status_code is not None else "no response"
        text = f"GET {url} failed after {attempts} attempt(s): {detail}"
        super().__init__(f"{text} ({message})" if message else text)


class ChecksumMismatchError(Exception):
    """A downloaded file's SHA-256 differs from the expected value."""


class TokenBucket:
    """Blocking token bucket. Starts full; refills at ``rate`` tokens/s up to ``capacity``.

    ``clock`` and ``sleep`` are injectable so tests run on fake time. ``acquire`` holds the
    lock while it waits, so concurrent callers are served one at a time and the rate holds
    across threads.
    """

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now

    def acquire(self) -> None:
        with self._lock:
            self._refill()
            if self._tokens < 1.0:
                self._sleep((1.0 - self._tokens) / self.rate)
                self._refill()
            # One wait, then take the token unconditionally. If float rounding left us a hair
            # under 1.0 the debt (about 1e-13 tokens) is repaid by the next wait; looping
            # here could spin forever when the remaining wait is below the clock's resolution.
            self._tokens -= 1.0


_shared_bucket: TokenBucket | None = None
_shared_lock = threading.Lock()


def _get_shared_bucket(rate: float) -> TokenBucket:
    """The process-wide bucket, created on first use at the first caller's ``rate``.

    A later caller asking for a different rate is an error: the bucket is shared, so one
    client silently running at another client's rate would hide a misconfiguration.
    """
    global _shared_bucket
    with _shared_lock:
        if _shared_bucket is None:
            _shared_bucket = TokenBucket(rate=rate)
        elif _shared_bucket.rate != rate:
            raise ValueError(
                f"the process-wide SEC bucket already runs at {_shared_bucket.rate} req/s; "
                f"cannot create a client at {rate} req/s (pass bucket= for a private one)"
            )
        return _shared_bucket


def _is_sec_https(url: httpx.URL) -> bool:
    """True for https URLs on sec.gov or a subdomain (parsed host, so lookalikes fail)."""
    host = (url.host or "").lower()
    return url.scheme == "https" and (host == "sec.gov" or host.endswith(".sec.gov"))


def _check_sec_url(url: str) -> None:
    """Reject anything that is not an https sec.gov URL before a token or header is spent."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"not a valid URL: {url!r} ({exc})") from exc
    if not _is_sec_https(parsed):
        raise ValueError(f"SecClient only fetches https://*.sec.gov URLs, got {url!r}")


def _resolve_user_agent(user_agent: str | None) -> str:
    ua = (user_agent if user_agent is not None else os.environ.get("SEC_USER_AGENT", "")).strip()
    if not ua:
        raise SecClientConfigError(
            "SEC_USER_AGENT is not set. The SEC requires a User-Agent with a name and contact "
            'email, e.g. SEC_USER_AGENT="Your Name you@example.org" (see .env.example).'
        )
    if any(p in ua.lower() for p in _PLACEHOLDERS):
        raise SecClientConfigError(
            "SEC_USER_AGENT still contains the placeholder from .env.example; "
            "put your real name and email in .env."
        )
    return ua


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), else None.

    Not capped here: the caller decides what to do with a wait longer than it will accept.
    """
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return max(seconds, 0.0)


class SecClient:
    """Rate-limited, retrying GET client for SEC endpoints."""

    def __init__(
        self,
        user_agent: str | None = None,
        rate: float = DEFAULT_RATE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        timeout: float = 30.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not 0 < rate <= MAX_RATE:
            raise ValueError(f"rate must be in (0, {MAX_RATE}] req/s (SEC access rules)")
        self._user_agent = _resolve_user_agent(user_agent)
        self._max_attempts = max_attempts
        if bucket is not None and bucket.rate > MAX_RATE:
            raise ValueError(f"bucket rate {bucket.rate} req/s exceeds the {MAX_RATE} req/s cap")
        self._bucket = bucket if bucket is not None else _get_shared_bucket(rate)
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._http = httpx.Client(
            headers={"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            # Redirects are followed by hand in _once: each hop is a real request that must
            # take a bucket token, and only https://*.sec.gov hops may carry our User-Agent.
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- public API --------------------------------------------------------------------

    def get(self, url: str) -> bytes:
        """GET ``url`` and return the (content-decoded) body."""
        return self._fetch(url, lambda response: response.read())

    def get_json(self, url: str) -> dict[str, Any]:
        """GET ``url`` and parse the body as a JSON object. Raises ``ValueError`` otherwise."""
        parsed = json.loads(self.get(url))
        if not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object from {url}, got {type(parsed).__name__}")
        return parsed

    def download(self, url: str, dest: Path, sha256: str | None = None) -> str:
        """Stream ``url`` to ``dest`` atomically; return the hex SHA-256 of what was written.

        The bytes go to a temp file in ``dest``'s directory (same filesystem, so the final
        rename is atomic). If ``sha256`` is given and differs, :class:`ChecksumMismatchError`
        is raised and any existing ``dest`` is left untouched. A transfer that dies mid-body
        is retried like any other transient failure, each time from a fresh temp file.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = sha256.strip().lower() if sha256 else None

        def write(response: httpx.Response) -> str:
            digest = hashlib.sha256()
            fd, tmp_name = tempfile.mkstemp(
                dir=dest.parent, prefix=f".{dest.name}.", suffix=".part"
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in response.iter_bytes():
                        digest.update(chunk)
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                hex_digest = digest.hexdigest()
                if expected is not None and hex_digest != expected:
                    raise ChecksumMismatchError(
                        f"{url}: expected sha256 {expected}, got {hex_digest}"
                    )
                tmp.chmod(0o644)
                os.replace(tmp, dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return hex_digest

        return self._fetch(url, write)

    # -- internals ---------------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Full jitter: uniform in [0, min(max, base * 2**attempt)], floored at Retry-After."""
        cap = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2**attempt)
        wait = self._rng.uniform(0.0, cap)
        return max(wait, retry_after) if retry_after is not None else wait

    def _redirect_target(
        self, origin: str, current: str, response: httpx.Response, attempt: int
    ) -> str:
        """The next URL for a 3xx response, or SecRequestError if it must not be followed."""
        location = response.headers.get("Location")
        if not location:
            raise SecRequestError(
                origin, response.status_code, attempt + 1, "redirect without Location"
            )
        target = response.url.join(location)
        if not _is_sec_https(target):
            # Our User-Agent carries a contact email; never send it (or spend a token) elsewhere.
            raise SecRequestError(
                origin,
                response.status_code,
                attempt + 1,
                f"refusing redirect to {target.scheme}://{target.host}",
            )
        return str(target)

    def _once(self, url: str, consume: Callable[[httpx.Response], T], attempt: int) -> T | _Retry:
        """One attempt: follow redirects by hand, then consume a 2xx body.

        Returns ``_Retry`` for transient failures; raises ``SecRequestError`` for permanent ones.
        Every request, redirect hops included, takes a bucket token first.
        """
        current = url
        status = 0
        try:
            for _ in range(MAX_REDIRECTS + 1):
                self._bucket.acquire()
                with self._http.stream("GET", current) as response:
                    status = response.status_code
                    if 200 <= status < 300:
                        return consume(response)
                    if status in _REDIRECT_STATUSES:
                        current = self._redirect_target(url, current, response, attempt)
                        continue
                    if status not in _RETRYABLE_STATUSES and status < 500:
                        raise SecRequestError(url, status, attempt + 1)  # will not improve
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    if retry_after is not None and retry_after > MAX_RETRY_AFTER_S:
                        raise SecRequestError(
                            url,
                            status,
                            attempt + 1,
                            f"Retry-After {retry_after:.0f}s exceeds the {MAX_RETRY_AFTER_S:.0f}s "
                            "limit; not retrying early",
                        )
                    return _Retry(status, f"HTTP {status}", retry_after)
        except (httpx.TransportError, httpx.DecodingError) as exc:
            # connect/read errors, timeouts, mid-body drops, corrupt gzip: transient
            return _Retry(None, f"{type(exc).__name__}: {exc}", None)
        raise SecRequestError(url, status, attempt + 1, f"more than {MAX_REDIRECTS} redirects")

    def _fetch(self, url: str, consume: Callable[[httpx.Response], T]) -> T:
        """GET with the shared retry policy; ``consume`` reads the body of a 2xx response."""
        _check_sec_url(url)  # a caller bug fails fast: no tokens, no retries, no User-Agent leak
        last = _Retry(None, "", None)
        for attempt in range(self._max_attempts):
            outcome = self._once(url, consume, attempt)
            if not isinstance(outcome, _Retry):
                return outcome
            last = outcome
            if attempt + 1 < self._max_attempts:
                wait = self._backoff(attempt, last.retry_after)
                log.warning(
                    "retrying %s after %.2fs (attempt %d/%d: %s)",
                    url,
                    wait,
                    attempt + 1,
                    self._max_attempts,
                    last.reason,
                )
                self._sleep(wait)
        raise SecRequestError(url, last.status, self._max_attempts, last.reason)
