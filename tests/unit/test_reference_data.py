"""Unit tests for ``pipelines/fetch/reference_data.py`` (the frozen ticker map and S&P 500 list).

Contract: ``docs/plan/05_data_contracts.md`` section 1.5 (``company_tickers.json`` and the dated
``data/sp500_<date>.csv``). Everything runs against ``respx`` mocks or ``httpx.MockTransport``, so
no test touches the network (CLAUDE.md rule 4). Two safety nets make an accidental real request
fail loudly instead of passing quietly: every router uses ``assert_all_mocked=True`` (an
unmocked URL raises), and an autouse fixture refuses ``socket.connect`` / ``getaddrinfo`` for
every host (the repo-wide guard only knows the SEC and Anthropic hosts, not raw.githubusercontent).

The SEC fetch goes through a real ``SecClient`` (rule 9) with an injected fake-clock bucket. The
S&P fetch is a plain GET to a non-SEC host and must never carry the SEC contact User-Agent.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipelines.fetch import reference_data as ref
from pipelines.fetch import sec_client as sec_client_mod
from pipelines.fetch.reference_data import (
    GENERIC_USER_AGENT,
    SP500_URL,
    TICKERS_URL,
    TickerRow,
    fetch_company_tickers,
    fetch_sp500,
    frozen_paths,
    main,
    parse_company_tickers,
)
from pipelines.fetch.sec_client import SecClient, SecRequestError, TokenBucket
from tests.fake_time import FakeTime

SEC_UA = "test-agent test@example.invalid"  # what the tests pass to SecClient(user_agent=...)
SECRET_UA = "Real Person real.person@contact-details.invalid"  # stands in for SEC_USER_AGENT

TICKERS_PAYLOAD: dict[str, Any] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": "789019", "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}

SP_HEADER = [
    "Symbol",
    "Security",
    "GICS Sector",
    "GICS Sub-Industry",
    "Headquarters Location",
    "Date added",
    "CIK",
    "Founded",
]


# --------------------------------------------------------------------------------------
# Builders and fixtures
# --------------------------------------------------------------------------------------


def sp_csv(rows: list[tuple[str, str]], header: list[str] | None = None) -> bytes:
    """A source-shaped S&P CSV (real header, quoted fields where needed) built with ``csv``.

    Columns other than Symbol/Security are filler. A ``header`` without Symbol or Security
    simply has no such column (the filler fills the cell), which is what the error tests need.
    """
    cols = header if header is not None else SP_HEADER
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for symbol, name in rows:
        record = {"Symbol": symbol, "Security": name}
        writer.writerow([record.get(c, "Cupertino, California") for c in cols])
    return buf.getvalue().encode("utf-8")


def generated_rows(n: int) -> list[tuple[str, str]]:
    return [(f"T{i:04d}", f"Company {i}") for i in range(n)]


def read_out(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def csv_ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "text/plain; charset=utf-8"})


def make_client(fake: FakeTime, user_agent: str = SEC_UA) -> SecClient:
    """A real SecClient with a private fake-clock bucket and no-op backoff sleeps."""
    bucket = TokenBucket(rate=8, clock=fake.clock, sleep=fake.bucket_sleep)
    return SecClient(
        user_agent=user_agent, bucket=bucket, sleep=fake.backoff_sleep, rng=random.Random(1234)
    )


def files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to open a socket or resolve a name (any host) fails the test."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a unit test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read a developer's .env or SEC_USER_AGENT; run from an empty directory."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr("dotenv.main.find_dotenv", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """Allows unused routes but fails any request to an unmocked URL."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


# --------------------------------------------------------------------------------------
# 1. parse_company_tickers
# --------------------------------------------------------------------------------------


def test_parse_returns_ticker_rows_with_rank_from_the_key():
    """Each entry becomes a TickerRow(cik, ticker, name=title, rank=int(key))."""
    rows = parse_company_tickers(TICKERS_PAYLOAD)

    assert rows == [
        TickerRow(cik=320193, ticker="AAPL", name="Apple Inc.", rank=0),
        TickerRow(cik=789019, ticker="MSFT", name="MICROSOFT CORP", rank=1),
        TickerRow(cik=1652044, ticker="GOOGL", name="Alphabet Inc.", rank=2),
    ]


def test_parse_sorts_by_integer_rank_not_by_key_text_or_dict_order():
    """Keys "9" and "10" sort numerically (9 before 10); a text sort would put "10" first."""
    data = {
        "10": {"cik_str": 3, "ticker": "CCC", "title": "C"},
        "2": {"cik_str": 2, "ticker": "BBB", "title": "B"},
        "100": {"cik_str": 4, "ticker": "DDD", "title": "D"},
        "9": {"cik_str": 1, "ticker": "AAA", "title": "A"},
    }

    rows = parse_company_tickers(data)

    assert [r.rank for r in rows] == [2, 9, 10, 100]
    assert [r.ticker for r in rows] == ["BBB", "AAA", "CCC", "DDD"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(320193, 320193), ("320193", 320193), ("0000320193", 320193), (1, 1)],
)
def test_parse_cik_str_may_be_int_or_numeric_string(raw: Any, expected: int):
    """The SEC file has used both; the row always carries a real int (leading zeros dropped)."""
    rows = parse_company_tickers({"0": {"cik_str": raw, "ticker": "X", "title": "X Corp"}})

    assert rows[0].cik == expected
    assert type(rows[0].cik) is int


def test_parse_keeps_every_row_of_a_multi_ticker_company():
    """GOOGL and GOOG share a CIK; both rows survive (build_companies derives the aliases)."""
    data = {
        "7": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
        "8": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    }

    rows = parse_company_tickers(data)

    assert [(r.cik, r.ticker, r.rank) for r in rows] == [
        (1652044, "GOOGL", 7),
        (1652044, "GOOG", 8),
    ]


def test_ticker_row_is_frozen_and_hashable():
    """Rows are plain value objects: usable in sets and not mutable."""
    row = TickerRow(cik=1, ticker="A", name="A Inc.", rank=0)

    assert {row, TickerRow(cik=1, ticker="A", name="A Inc.", rank=0)} == {row}
    with pytest.raises(AttributeError):
        row.ticker = "B"  # type: ignore[misc]


@pytest.mark.parametrize("missing", ["cik_str", "ticker", "title"])
def test_parse_entry_missing_a_field_raises_value_error(missing: str):
    """A malformed entry is a loud ValueError (not KeyError, not a silently dropped row)."""
    entry = {"cik_str": 1, "ticker": "A", "title": "A Inc."}
    del entry[missing]
    data = {"0": {"cik_str": 2, "ticker": "B", "title": "B Inc."}, "1": entry}

    with pytest.raises(ValueError):
        parse_company_tickers(data)


def test_parse_empty_dict_raises_value_error():
    """An empty map means a wrong download (an error page parsed as {}), not 'no companies'."""
    with pytest.raises(ValueError):
        parse_company_tickers({})


@pytest.mark.parametrize("bad_cik", ["not-a-number", "", None])
def test_parse_non_numeric_cik_raises_value_error(bad_cik: Any):
    """A cik_str that is not an integer cannot become a CIK; it must not be coerced or skipped."""
    with pytest.raises(ValueError):
        parse_company_tickers({"0": {"cik_str": bad_cik, "ticker": "A", "title": "A"}})


@pytest.mark.parametrize("field", ["ticker", "title"])
@pytest.mark.parametrize("bad", [None, "", "   ", 5])
def test_parse_rejects_a_null_blank_or_non_string_ticker_or_title(field: str, bad: Any):
    """`str(None)` would store the text 'None' as a ticker, and a blank ticker would become
    curated.companies.ticker; both are malformed entries and must fail loudly."""
    entry = {"cik_str": 1, "ticker": "A", "title": "A Inc.", field: bad}

    with pytest.raises(ValueError):
        parse_company_tickers({"0": entry})


@pytest.mark.parametrize("bad_cik", [320193.9, 320193.0, True])
def test_parse_rejects_a_float_or_bool_cik_instead_of_truncating_it(bad_cik: Any):
    """int(320193.9) is 320193: a lossy coercion would attach the row to the wrong company."""
    with pytest.raises(ValueError):
        parse_company_tickers({"0": {"cik_str": bad_cik, "ticker": "A", "title": "A"}})


def test_parse_non_integer_key_raises_value_error():
    """The key is the rank, so a non-numeric key cannot be ranked (inferred from the spec)."""
    with pytest.raises(ValueError):
        parse_company_tickers({"abc": {"cik_str": 1, "ticker": "A", "title": "A"}})


# --------------------------------------------------------------------------------------
# 2. fetch_company_tickers
# --------------------------------------------------------------------------------------


def test_fetch_company_tickers_writes_the_original_json_and_returns_dest(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """The file holds the SEC document as served (str cik_str stays a str), not our parsed rows."""
    route = router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    dest = tmp_path / "company_tickers.json"

    with make_client(fake) as client:
        result = fetch_company_tickers(client, dest)

    assert result == dest
    assert json.loads(dest.read_text(encoding="utf-8")) == TICKERS_PAYLOAD
    assert json.loads(dest.read_text(encoding="utf-8"))["1"]["cik_str"] == "789019"
    assert route.call_count == 1
    assert files_in(tmp_path) == ["company_tickers.json"]


def test_fetch_company_tickers_sends_the_sec_user_agent(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """The request carries the User-Agent the SecClient was built with (CLAUDE.md rule 9)."""
    route = router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))

    with make_client(fake) as client:
        fetch_company_tickers(client, tmp_path / "t.json")

    assert route.calls[0].request.headers["User-Agent"] == SEC_UA


def test_fetch_company_tickers_creates_missing_parent_directories(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """A fresh checkout has no data/ directory yet."""
    router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    dest = tmp_path / "a" / "b" / "company_tickers_2026-11-16.json"

    with make_client(fake) as client:
        fetch_company_tickers(client, dest)

    assert dest.is_file()


def test_fetch_company_tickers_invalid_payload_raises_and_writes_nothing(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """A document that parse_company_tickers rejects is never frozen to disk."""
    router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json={}))
    dest = tmp_path / "out" / "t.json"

    with make_client(fake) as client, pytest.raises(ValueError):
        fetch_company_tickers(client, dest)

    assert not dest.exists()
    assert files_in(tmp_path / "out") == []


def test_fetch_company_tickers_invalid_payload_keeps_an_existing_dest(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """A bad refresh must not clobber a previous good snapshot."""
    router.get(TICKERS_URL).mock(
        return_value=httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "A"}})
    )
    dest = tmp_path / "t.json"
    dest.write_text('{"keep": "me"}', encoding="utf-8")

    with make_client(fake) as client, pytest.raises(ValueError):
        fetch_company_tickers(client, dest)

    assert dest.read_text(encoding="utf-8") == '{"keep": "me"}'
    assert files_in(tmp_path) == ["t.json"]


def test_fetch_company_tickers_404_raises_and_leaves_no_file(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """An HTTP failure surfaces as SecRequestError and leaves neither dest nor a temp file."""
    router.get(TICKERS_URL).mock(return_value=httpx.Response(404))
    dest = tmp_path / "out" / "t.json"

    with make_client(fake) as client, pytest.raises(SecRequestError) as info:
        fetch_company_tickers(client, dest)

    assert info.value.status_code == 404
    assert not dest.exists()
    assert files_in(tmp_path / "out") == []


def test_fetch_company_tickers_replaces_existing_dest_via_same_dir_rename(
    fake: FakeTime,
    router: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Success swaps the file in with one rename from a temp file in dest's own directory.

    Same directory matters because a rename is atomic only within one filesystem. No temp file
    remains, and the old content is replaced.
    """
    router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    dest = tmp_path / "sub" / "t.json"
    dest.parent.mkdir()
    dest.write_text('{"old": true}', encoding="utf-8")
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

    with make_client(fake) as client:
        fetch_company_tickers(client, dest)

    final = [(s, d) for s, d in renames if d == dest]
    assert len(final) == 1, f"expected one rename onto dest, saw {renames}"
    src, _ = final[0]
    assert src != dest
    assert src.parent == dest.parent
    assert json.loads(dest.read_text(encoding="utf-8")) == TICKERS_PAYLOAD
    assert files_in(dest.parent) == ["t.json"]


# --------------------------------------------------------------------------------------
# 3. fetch_sp500: normalization
# --------------------------------------------------------------------------------------


def test_fetch_sp500_writes_ticker_name_csv_sorted_by_ticker(
    router: respx.MockRouter, tmp_path: Path
):
    """Output header is exactly ``ticker,name``; rows are sorted by ticker; extra columns drop."""
    body = sp_csv([("MSFT", "Microsoft"), ("A", "Agilent"), ("AAPL", "Apple Inc."), ("BF.B", "BF")])
    router.get(SP500_URL).mock(return_value=csv_ok(body))
    dest = tmp_path / "sp500.csv"

    result = fetch_sp500(dest, min_rows=4)

    assert result == dest
    assert dest.read_text(encoding="utf-8").splitlines()[0] == "ticker,name"
    assert read_out(dest) == [
        ["ticker", "name"],
        ["A", "Agilent"],
        ["AAPL", "Apple Inc."],
        ["BF.B", "BF"],
        ["MSFT", "Microsoft"],
    ]


def test_fetch_sp500_names_with_commas_and_quotes_round_trip(
    router: respx.MockRouter, tmp_path: Path
):
    """Quoted source fields with commas or quotes parse as one name and are re-quoted on output."""
    tricky = 'Berkshire Hathaway Inc., Class B ("BRK")'
    router.get(SP500_URL).mock(
        return_value=csv_ok(sp_csv([("BRK.B", tricky), ("AAPL", "Apple Inc.")]))
    )
    dest = tmp_path / "sp500.csv"

    fetch_sp500(dest, min_rows=2)

    assert read_out(dest) == [["ticker", "name"], ["AAPL", "Apple Inc."], ["BRK.B", tricky]]


def test_fetch_sp500_keeps_the_source_ticker_spelling(router: respx.MockRouter, tmp_path: Path):
    """The file records the list as published ("BRK.B"); dot-to-dash normalization is the
    consumer's job (build_companies.normalize_ticker), so the frozen snapshot stays faithful."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("BRK.B", "Berkshire")])))
    dest = tmp_path / "sp500.csv"

    fetch_sp500(dest, min_rows=1)

    assert read_out(dest)[1][0] == "BRK.B"


def test_fetch_sp500_finds_columns_by_header_name_not_position(
    router: respx.MockRouter, tmp_path: Path
):
    """A reordered source header (Security before Symbol, extra columns) still maps correctly."""
    header = ["CIK", "Security", "Founded", "Symbol"]
    router.get(SP500_URL).mock(
        return_value=csv_ok(sp_csv([("AAPL", "Apple Inc."), ("MSFT", "Microsoft")], header))
    )
    dest = tmp_path / "sp500.csv"

    fetch_sp500(dest, min_rows=2)

    assert read_out(dest) == [["ticker", "name"], ["AAPL", "Apple Inc."], ["MSFT", "Microsoft"]]


def test_fetch_sp500_non_ascii_names_survive_as_utf8(router: respx.MockRouter, tmp_path: Path):
    """Names with accents or symbols are written and read back as UTF-8, not mangled."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("LIN", "Linde plc é€")])))
    dest = tmp_path / "sp500.csv"

    fetch_sp500(dest, min_rows=1)

    assert read_out(dest)[1] == ["LIN", "Linde plc é€"]


def test_fetch_sp500_creates_missing_parent_directories(router: respx.MockRouter, tmp_path: Path):
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")])))
    dest = tmp_path / "data" / "deep" / "sp500_2026-11-16.csv"

    fetch_sp500(dest, min_rows=1)

    assert dest.is_file()


def test_fetch_sp500_honors_a_custom_url(router: respx.MockRouter, tmp_path: Path):
    """``url=`` replaces the default source (the default route is unmocked, so using it fails)."""
    other = "https://example.invalid/constituents.csv"
    route = router.get(other).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")])))

    fetch_sp500(tmp_path / "s.csv", url=other, min_rows=1)

    assert route.call_count == 1


# --------------------------------------------------------------------------------------
# 4. fetch_sp500: the generic User-Agent (the SEC contact string must never leave for GitHub)
# --------------------------------------------------------------------------------------


def _headers_and_url_text(request: httpx.Request) -> str:
    return "\n".join([str(request.url), *(f"{k}: {v}" for k, v in request.headers.items())])


def test_fetch_sp500_sends_the_generic_user_agent(
    router: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The only UA sent to a non-SEC host is GENERIC_USER_AGENT, even with SEC_USER_AGENT set.

    SEC_USER_AGENT holds a person's name and email; it is for sec.gov only.
    """
    monkeypatch.setenv("SEC_USER_AGENT", SECRET_UA)
    route = router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")])))

    fetch_sp500(tmp_path / "s.csv", min_rows=1)

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.method == "GET"
    assert request.headers["User-Agent"] == GENERIC_USER_AGENT
    assert SECRET_UA not in _headers_and_url_text(request)
    assert "contact-details" not in _headers_and_url_text(request)


def test_generic_user_agent_constant_has_no_contact_details():
    """The constant itself must not smuggle in an email address."""
    assert GENERIC_USER_AGENT == "edgar-verified-qa/0.1 (portfolio project)"
    assert "@" not in GENERIC_USER_AGENT


def test_sec_and_github_requests_in_one_run_keep_their_user_agents_apart(
    fake: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """SEC request: the SecClient UA. GitHub request: the generic UA and no trace of the SEC one."""
    sec = router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    gh = router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")])))

    with make_client(fake, SEC_UA) as client:
        fetch_company_tickers(client, tmp_path / "t.json")
    fetch_sp500(tmp_path / "s.csv", min_rows=1)

    assert sec.calls[0].request.headers["User-Agent"] == SEC_UA
    assert gh.calls[0].request.headers["User-Agent"] == GENERIC_USER_AGENT
    assert SEC_UA not in _headers_and_url_text(gh.calls[0].request)
    assert "test@example.invalid" not in _headers_and_url_text(gh.calls[0].request)


def test_fetch_sp500_uses_the_injected_client_and_still_sets_the_generic_user_agent(
    router: respx.MockRouter, tmp_path: Path
):
    """An injected client is the one that makes the request, but its own default User-Agent
    (here a contact-bearing one) must not be what the server sees."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return csv_ok(sp_csv([("AAPL", "Apple Inc.")]))

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": SECRET_UA})

    fetch_sp500(tmp_path / "s.csv", http=client, min_rows=1)

    assert len(seen) == 1
    assert seen[0].headers["User-Agent"] == GENERIC_USER_AGENT
    assert len(router.calls) == 0  # the global network path was not used


# --------------------------------------------------------------------------------------
# 5. fetch_sp500: validation errors
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        ["Security", "GICS Sector"],
        ["Symbol", "GICS Sector"],
        ["Ticker", "Name"],
    ],
    ids=["no_symbol", "no_security", "neither"],
)
def test_fetch_sp500_header_missing_symbol_or_security_raises_and_writes_nothing(
    header: list[str], router: respx.MockRouter, tmp_path: Path
):
    """Without Symbol and Security the schema changed upstream: stop, don't write a wrong file."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")], header)))
    dest = tmp_path / "out" / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=1)

    assert not dest.exists()
    assert files_in(tmp_path / "out") == []


def test_fetch_sp500_empty_body_raises_value_error(router: respx.MockRouter, tmp_path: Path):
    """A 200 with no content has no header at all."""
    router.get(SP500_URL).mock(return_value=csv_ok(b""))
    dest = tmp_path / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=1)

    assert not dest.exists()


def test_fetch_sp500_html_error_page_served_with_200_raises(
    router: respx.MockRouter, tmp_path: Path
):
    """An HTML page where the CSV should be has no Symbol/Security header."""
    router.get(SP500_URL).mock(return_value=csv_ok(b"<html><body>Rate limited</body></html>"))
    dest = tmp_path / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=1)

    assert not dest.exists()


def test_fetch_sp500_default_min_rows_is_400(router: respx.MockRouter, tmp_path: Path):
    """399 data rows fail and 400 pass by default, so a truncated download is never frozen."""
    route = router.get(SP500_URL)
    dest = tmp_path / "s.csv"

    route.mock(return_value=csv_ok(sp_csv(generated_rows(399))))
    with pytest.raises(ValueError):
        fetch_sp500(dest)
    assert not dest.exists()

    route.mock(return_value=csv_ok(sp_csv(generated_rows(400))))
    fetch_sp500(dest)
    assert len(read_out(dest)) == 401  # header + 400


def test_fetch_sp500_min_rows_boundary(router: respx.MockRouter, tmp_path: Path):
    """``min_rows`` is inclusive: 3 rows satisfy min_rows=3 and fail min_rows=4."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv(generated_rows(3))))
    dest = tmp_path / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=4)
    assert not dest.exists()

    fetch_sp500(dest, min_rows=3)
    assert len(read_out(dest)) == 4


def test_fetch_sp500_header_only_counts_as_zero_rows(router: respx.MockRouter, tmp_path: Path):
    """A header with no data rows is too few rows, not an empty success."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([])))
    dest = tmp_path / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=1)

    assert not dest.exists()


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_fetch_sp500_blank_symbol_raises_and_writes_nothing(
    blank: str, router: respx.MockRouter, tmp_path: Path
):
    """A row with no ticker cannot be matched to anything; one bad row rejects the whole file."""
    rows = [("AAPL", "Apple Inc."), (blank, "Mystery Corp"), ("MSFT", "Microsoft")]
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv(rows)))
    dest = tmp_path / "out" / "s.csv"

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=3)

    assert not dest.exists()
    assert files_in(tmp_path / "out") == []


def test_fetch_sp500_validation_failure_keeps_an_existing_dest(
    router: respx.MockRouter, tmp_path: Path
):
    """A truncated refresh must leave the previous good snapshot byte-for-byte as it was."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv(generated_rows(3))))
    dest = tmp_path / "s.csv"
    dest.write_text("ticker,name\nOLD,Old Co\n", encoding="utf-8")

    with pytest.raises(ValueError):
        fetch_sp500(dest, min_rows=400)

    assert dest.read_text(encoding="utf-8") == "ticker,name\nOLD,Old Co\n"
    assert files_in(tmp_path) == ["s.csv"]


# --------------------------------------------------------------------------------------
# 6. fetch_sp500: HTTP failure and atomic write
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 500, 503])
def test_fetch_sp500_non_2xx_raises_and_leaves_no_file(
    status: int, router: respx.MockRouter, tmp_path: Path
):
    """Any non-2xx is an error; neither dest nor a temp file may appear."""
    router.get(SP500_URL).mock(return_value=httpx.Response(status))
    dest = tmp_path / "out" / "s.csv"

    with pytest.raises(httpx.HTTPError):
        fetch_sp500(dest, min_rows=1)

    assert not dest.exists()
    assert files_in(tmp_path / "out") == []


def test_fetch_sp500_http_failure_keeps_an_existing_dest(router: respx.MockRouter, tmp_path: Path):
    """A failed refresh never truncates the snapshot already on disk."""
    router.get(SP500_URL).mock(return_value=httpx.Response(404))
    dest = tmp_path / "s.csv"
    dest.write_text("ticker,name\nOLD,Old Co\n", encoding="utf-8")

    with pytest.raises(httpx.HTTPError):
        fetch_sp500(dest, min_rows=1)

    assert dest.read_text(encoding="utf-8") == "ticker,name\nOLD,Old Co\n"
    assert files_in(tmp_path) == ["s.csv"]


def test_fetch_sp500_replaces_existing_dest_via_same_dir_rename(
    router: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same atomic-write rule as the SEC file: one rename from a sibling temp file, no debris."""
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv([("AAPL", "Apple Inc.")])))
    dest = tmp_path / "sub" / "s.csv"
    dest.parent.mkdir()
    dest.write_text("ticker,name\nOLD,Old Co\n", encoding="utf-8")
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

    fetch_sp500(dest, min_rows=1)

    final = [(s, d) for s, d in renames if d == dest]
    assert len(final) == 1, f"expected one rename onto dest, saw {renames}"
    assert final[0][0] != dest
    assert final[0][0].parent == dest.parent
    assert read_out(dest) == [["ticker", "name"], ["AAPL", "Apple Inc."]]
    assert files_in(dest.parent) == ["s.csv"]


# --------------------------------------------------------------------------------------
# 7. frozen_paths
# --------------------------------------------------------------------------------------


def test_frozen_paths_names_follow_the_contract(tmp_path: Path):
    """(company_tickers_<iso>.json, sp500_<iso>.csv) inside the directory (contract section 1.5)."""
    assert frozen_paths(tmp_path, date(2026, 11, 16)) == (
        tmp_path / "company_tickers_2026-11-16.json",
        tmp_path / "sp500_2026-11-16.csv",
    )


def test_frozen_paths_zero_pads_month_and_day(tmp_path: Path):
    """isoformat padding: 2026-01-05, not 2026-1-5."""
    tickers, sp500 = frozen_paths(tmp_path, date(2026, 1, 5))

    assert tickers.name == "company_tickers_2026-01-05.json"
    assert sp500.name == "sp500_2026-01-05.csv"


# --------------------------------------------------------------------------------------
# 8. main: patched fetch functions
# --------------------------------------------------------------------------------------


class Recorder:
    """Records calls to the patched fetch functions and writes distinguishable content."""

    def __init__(self) -> None:
        self.clients: list[Any] = []
        self.tickers_calls: list[tuple[Any, Path]] = []
        self.sp500_calls: list[Path] = []

    @property
    def total(self) -> int:
        return len(self.tickers_calls) + len(self.sp500_calls)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Patch SecClient and both fetch functions on the module under test (they are the
    boundary here; main itself is the unit under test and is not mocked)."""
    recorder = Recorder()

    class FakeSecClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            recorder.clients.append(self)

        def close(self) -> None:
            pass

        def __enter__(self) -> FakeSecClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def fake_tickers(client: Any, dest: Path) -> Path:
        recorder.tickers_calls.append((client, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"fresh-tickers-{len(recorder.tickers_calls)}", encoding="utf-8")
        return dest

    def fake_sp500(dest: Path, **kwargs: Any) -> Path:
        recorder.sp500_calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"fresh-sp500-{len(recorder.sp500_calls)}", encoding="utf-8")
        return dest

    monkeypatch.setattr(ref, "SecClient", FakeSecClient)
    monkeypatch.setattr(ref, "fetch_company_tickers", fake_tickers)
    monkeypatch.setattr(ref, "fetch_sp500", fake_sp500)
    return recorder


def run_main(argv: list[str]) -> int:
    """Exit status of main(argv), whether it returns or exits (argparse raises SystemExit)."""
    try:
        status = main(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return status


DAY = date(2026, 11, 16)


def test_main_fetches_both_files_to_the_frozen_paths(rec: Recorder, tmp_path: Path):
    """A first run fetches each file once, to exactly frozen_paths(dest, date), and returns 0."""
    dest = tmp_path / "snap" / "nested"
    tickers_path, sp500_path = frozen_paths(dest, DAY)

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    assert status == 0
    assert [d for _, d in rec.tickers_calls] == [tickers_path]
    assert rec.sp500_calls == [sp500_path]
    assert tickers_path.read_text(encoding="utf-8") == "fresh-tickers-1"
    assert sp500_path.read_text(encoding="utf-8") == "fresh-sp500-1"
    assert files_in(dest) == sorted([tickers_path.name, sp500_path.name])


def test_main_passes_one_sec_client_to_the_tickers_fetch(rec: Recorder, tmp_path: Path):
    """The SEC file goes through the SecClient main builds (rule 9), not some other HTTP path."""
    run_main(["--dest", str(tmp_path / "d"), "--date", DAY.isoformat()])

    assert len(rec.clients) == 1
    assert rec.tickers_calls[0][0] is rec.clients[0]


def test_main_default_dest_is_data_under_the_working_directory(rec: Recorder, tmp_path: Path):
    """With no --dest the files land in ./data (cwd is an empty tmp dir here)."""
    assert run_main(["--date", DAY.isoformat()]) == 0

    tickers_path, sp500_path = frozen_paths(tmp_path / "data", DAY)
    assert tickers_path.is_file()
    assert sp500_path.is_file()


def test_main_default_date_is_today(rec: Recorder, tmp_path: Path):
    """With no --date the snapshot is named for today (checked on both sides of the call so a
    midnight rollover cannot flake the test)."""
    dest = tmp_path / "d"
    before = date.today()

    assert run_main(["--dest", str(dest)]) == 0
    after = date.today()

    candidates = [sorted(p.name for p in frozen_paths(dest, day)) for day in (before, after)]
    assert files_in(dest) in candidates


def test_main_skips_both_fetches_when_snapshots_exist(
    rec: Recorder,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    """Frozen snapshots are never overwritten silently: no refetch, content untouched, exit 0,
    and the skip is reported (log or output) so it is not silent."""
    dest = tmp_path / "d"
    tickers_path, sp500_path = frozen_paths(dest, DAY)
    dest.mkdir()
    tickers_path.write_text("FROZEN-T", encoding="utf-8")
    sp500_path.write_text("FROZEN-S", encoding="utf-8")
    caplog.set_level(logging.INFO)

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    assert status == 0
    assert rec.total == 0
    assert tickers_path.read_text(encoding="utf-8") == "FROZEN-T"
    assert sp500_path.read_text(encoding="utf-8") == "FROZEN-S"
    captured = capsys.readouterr()
    reported = f"{captured.out}\n{captured.err}\n{caplog.text}"
    assert tickers_path.name in reported
    assert sp500_path.name in reported


@pytest.mark.parametrize("existing", ["tickers", "sp500"])
def test_main_skips_only_the_file_that_exists(existing: str, rec: Recorder, tmp_path: Path):
    """The skip is per file: the missing snapshot is fetched, the existing one is left alone."""
    dest = tmp_path / "d"
    tickers_path, sp500_path = frozen_paths(dest, DAY)
    dest.mkdir()
    present = tickers_path if existing == "tickers" else sp500_path
    present.write_text("FROZEN", encoding="utf-8")

    assert run_main(["--dest", str(dest), "--date", DAY.isoformat()]) == 0

    assert present.read_text(encoding="utf-8") == "FROZEN"
    assert len(rec.tickers_calls) == (0 if existing == "tickers" else 1)
    assert len(rec.sp500_calls) == (0 if existing == "sp500" else 1)
    assert tickers_path.is_file()
    assert sp500_path.is_file()


def test_main_refresh_refetches_existing_snapshots(rec: Recorder, tmp_path: Path):
    """--refresh is the one way to replace a frozen snapshot."""
    dest = tmp_path / "d"
    tickers_path, sp500_path = frozen_paths(dest, DAY)
    dest.mkdir()
    tickers_path.write_text("FROZEN-T", encoding="utf-8")
    sp500_path.write_text("FROZEN-S", encoding="utf-8")

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat(), "--refresh"])

    assert status == 0
    assert len(rec.tickers_calls) == 1
    assert len(rec.sp500_calls) == 1
    assert tickers_path.read_text(encoding="utf-8") == "fresh-tickers-1"
    assert sp500_path.read_text(encoding="utf-8") == "fresh-sp500-1"


def test_main_snapshots_are_per_date(rec: Recorder, tmp_path: Path):
    """An older dated snapshot neither blocks today's fetch nor gets touched by it."""
    dest = tmp_path / "d"
    old_tickers, old_sp500 = frozen_paths(dest, date(2026, 11, 15))
    dest.mkdir()
    old_tickers.write_text("OLD-T", encoding="utf-8")
    old_sp500.write_text("OLD-S", encoding="utf-8")

    assert run_main(["--dest", str(dest), "--date", DAY.isoformat()]) == 0

    new_tickers, new_sp500 = frozen_paths(dest, DAY)
    assert new_tickers.is_file()
    assert new_sp500.is_file()
    assert old_tickers.read_text(encoding="utf-8") == "OLD-T"
    assert old_sp500.read_text(encoding="utf-8") == "OLD-S"


@pytest.mark.parametrize("bad", ["2026-13-45", "11/16/2026", "yesterday", "20261116x", ""])
def test_main_rejects_a_malformed_date_before_fetching(bad: str, rec: Recorder, tmp_path: Path):
    """--date must be ISO; anything else is a usage error and nothing is fetched or written
    (inferred from 'ISO'; the spec does not name the exit code, only that it must not succeed)."""
    status = run_main(["--dest", str(tmp_path / "d"), "--date", bad])

    assert status != 0
    assert rec.total == 0
    assert not (tmp_path / "d").exists()


# --------------------------------------------------------------------------------------
# 9. main end to end through the real fetch functions (HTTP mocked)
# --------------------------------------------------------------------------------------


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch, fake: FakeTime) -> FakeTime:
    """SEC_USER_AGENT set, and the process-wide bucket swapped for a fake-clock one so the real
    SecClient that main builds never really waits."""
    monkeypatch.setenv("SEC_USER_AGENT", SECRET_UA)
    monkeypatch.setattr(
        sec_client_mod,
        "_shared_bucket",
        TokenBucket(rate=8, clock=fake.clock, sleep=fake.bucket_sleep),
    )
    return fake


def test_main_end_to_end_keeps_the_sec_contact_ua_off_the_github_request(
    real_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """The whole path with only HTTP mocked: SEC gets SEC_USER_AGENT, GitHub gets the generic
    one and never sees the contact string, and both files land at the frozen paths."""
    sec = router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    gh = router.get(SP500_URL).mock(return_value=csv_ok(sp_csv(generated_rows(450))))
    dest = tmp_path / "data"

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    tickers_path, sp500_path = frozen_paths(dest, DAY)
    assert status == 0
    assert sec.call_count == 1
    assert gh.call_count == 1
    assert sec.calls[0].request.headers["User-Agent"] == SECRET_UA
    assert gh.calls[0].request.headers["User-Agent"] == GENERIC_USER_AGENT
    assert "contact-details" not in _headers_and_url_text(gh.calls[0].request)
    assert json.loads(tickers_path.read_text(encoding="utf-8")) == TICKERS_PAYLOAD
    assert len(read_out(sp500_path)) == 451
    assert files_in(dest) == sorted([tickers_path.name, sp500_path.name])


def test_main_end_to_end_rerun_makes_no_request_and_changes_nothing(
    real_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """Idempotent: the second identical run is a no-op (no HTTP, same bytes), exit 0."""
    router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    router.get(SP500_URL).mock(return_value=csv_ok(sp_csv(generated_rows(450))))
    argv = ["--dest", str(tmp_path / "data"), "--date", DAY.isoformat()]
    assert run_main(argv) == 0
    before = {p.name: p.read_bytes() for p in (tmp_path / "data").iterdir()}
    router.reset()

    assert run_main(argv) == 0

    assert len(router.calls) == 0
    assert {p.name: p.read_bytes() for p in (tmp_path / "data").iterdir()} == before


# --------------------------------------------------------------------------------------
# 10. main reports failures with an exit status, like download_fsds (2 = configuration,
#     1 = a fetch failed), instead of a traceback
# --------------------------------------------------------------------------------------


def test_main_without_a_sec_user_agent_exits_2_and_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, router: respx.MockRouter, tmp_path: Path, capsys
):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    dest = tmp_path / "data"

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    assert status == 2
    assert len(router.calls) == 0
    assert files_in(dest) == []
    assert "SEC_USER_AGENT" in capsys.readouterr().err


def test_main_exits_1_when_the_sec_fetch_fails_and_writes_nothing(
    real_env: FakeTime, router: respx.MockRouter, tmp_path: Path, capsys
):
    router.get(TICKERS_URL).mock(return_value=httpx.Response(404))
    dest = tmp_path / "data"

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    assert status == 1
    assert files_in(dest) == []
    assert "404" in capsys.readouterr().err


def test_main_exits_1_when_the_sp500_fetch_fails(
    real_env: FakeTime, router: respx.MockRouter, tmp_path: Path, capsys
):
    """The ticker map was frozen before the S&P request failed; that file is valid and kept, the
    S&P file is absent, and a rerun (without --refresh) fetches only what is missing."""
    router.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS_PAYLOAD))
    router.get(SP500_URL).mock(return_value=httpx.Response(503))
    dest = tmp_path / "data"

    status = run_main(["--dest", str(dest), "--date", DAY.isoformat()])

    assert status == 1
    assert files_in(dest) == [frozen_paths(dest, DAY)[0].name]
    assert "503" in capsys.readouterr().err
