"""Unit tests for ``pipelines/fetch/list_filings.py`` (M02 section 3.1: the filing target list).

The module turns SEC submissions JSON (``filings.recent`` plus the older pages named in
``filings.files``) into ``data/targets/filings.csv``. Every request goes through a real
``SecClient`` (CLAUDE.md rule 9) whose HTTP layer is a ``respx`` mock, so no test touches the
network (rule 4). The router fails any request to an unmocked URL.
"""

from __future__ import annotations

import csv
import random
import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipelines.fetch.list_filings import (
    FILING_FORMS,
    FilingTarget,
    SubmissionsFormatError,
    collect_cik,
    main,
    parse_block,
    render_summary,
    summarize,
    write_targets,
)
from pipelines.fetch.sec_client import SecClient, TokenBucket
from tests.fake_time import FakeTime

SEC_UA = "test-agent test@example.invalid"
FROM = date(2020, 1, 1)
HEADER = ["cik", "accession", "form", "filed", "report_date", "primary_doc", "url"]


def block(*rows: tuple[str, str, str, str, str]) -> dict[str, list[str]]:
    """A columnar submissions block from (accession, form, filed, report, primary_doc) rows."""
    cols = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    return {c: [r[i] for r in rows] for i, c in enumerate(cols)}


def submissions(
    recent: dict[str, list[str]], files: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"cik": "320193", "filings": {"recent": recent, "files": files or []}}


AAPL_10K = ("0000320193-23-000106", "10-K", "2023-11-03", "2023-09-30", "aapl-20230930.htm")
AAPL_10Q = ("0000320193-23-000077", "10-Q", "2023-08-04", "2023-07-01", "aapl-20230701.htm")


def make_client(fake: FakeTime) -> SecClient:
    bucket = TokenBucket(rate=8, clock=fake.clock, sleep=fake.bucket_sleep)
    return SecClient(
        user_agent=SEC_UA, bucket=bucket, sleep=fake.backoff_sleep, rng=random.Random(1234)
    )


def read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a unit test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr("dotenv.main.find_dotenv", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


# --------------------------------------------------------------------------------------
# parse_block: one columnar block -> targets
# --------------------------------------------------------------------------------------


def test_form_set_is_exactly_the_four_annual_and_quarterly_forms():
    assert FILING_FORMS == frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})


def test_parse_block_builds_the_document_url_from_cik_accession_and_primary_doc():
    """Leading zeros are dropped from the CIK, the dashes from the accession (contract 1.3)."""
    result = parse_block(320193, block(AAPL_10K), from_date=FROM)

    assert result.targets == [
        FilingTarget(
            cik=320193,
            accession="0000320193-23-000106",
            form="10-K",
            filed=date(2023, 11, 3),
            report_date=date(2023, 9, 30),
            primary_doc="aapl-20230930.htm",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm",
        )
    ]


@pytest.mark.parametrize("form", ["8-K", "10-K405", "10-KT", "NT 10-K", "DEF 14A", "10-Q405", ""])
def test_parse_block_drops_every_other_form(form: str):
    row = ("0000320193-23-000999", form, "2023-11-03", "2023-09-30", "x.htm")

    assert parse_block(320193, block(row), from_date=FROM).targets == []


@pytest.mark.parametrize("form", ["10-K", "10-K/A", "10-Q", "10-Q/A"])
def test_parse_block_keeps_the_four_forms(form: str):
    row = ("0000320193-23-000999", form, "2023-11-03", "2023-09-30", "x.htm")

    assert [t.form for t in parse_block(320193, block(row), from_date=FROM).targets] == [form]


def test_report_date_cutoff_is_inclusive_on_the_from_date():
    rows = [
        ("0000320193-20-000001", "10-K", "2020-02-01", "2019-12-31", "a.htm"),
        ("0000320193-20-000002", "10-K", "2020-02-02", "2020-01-01", "b.htm"),
    ]

    result = parse_block(320193, block(*rows), from_date=FROM)

    assert [t.accession for t in result.targets] == ["0000320193-20-000002"]


def test_to_date_cutoff_is_inclusive_and_optional():
    rows = [
        ("0000320193-25-000001", "10-K", "2025-11-01", "2025-09-27", "a.htm"),
        ("0000320193-26-000001", "10-Q", "2026-02-01", "2025-12-27", "b.htm"),
    ]

    capped = parse_block(320193, block(*rows), from_date=FROM, to_date=date(2025, 9, 27))
    open_ended = parse_block(320193, block(*rows), from_date=FROM)

    assert [t.accession for t in capped.targets] == ["0000320193-25-000001"]
    assert len(open_ended.targets) == 2


def test_blank_report_date_falls_back_to_filed_for_the_cutoff_and_stays_blank():
    old = ("0000320193-19-000001", "10-K", "2019-11-01", "", "a.htm")
    new = ("0000320193-21-000001", "10-K", "2021-11-01", "", "b.htm")

    result = parse_block(320193, block(old, new), from_date=FROM)

    assert [(t.accession, t.report_date) for t in result.targets] == [
        ("0000320193-21-000001", None)
    ]


def test_a_missing_primary_document_is_skipped_and_counted_not_silently_lost():
    row = ("0000320193-23-000106", "10-K", "2023-11-03", "2023-09-30", "")

    result = parse_block(320193, block(row), from_date=FROM)

    assert result.targets == []
    assert result.skipped_no_primary_doc == 1


def test_out_of_scope_forms_and_dates_are_not_counted_as_skipped_problems():
    rows = [
        ("0000320193-23-000001", "8-K", "2023-01-01", "2023-01-01", ""),
        ("0000320193-18-000001", "10-K", "2018-11-01", "2018-09-29", ""),
    ]

    assert parse_block(320193, block(*rows), from_date=FROM).skipped_no_primary_doc == 0


def test_mismatched_column_lengths_are_an_error():
    broken = block(AAPL_10K, AAPL_10Q)
    broken["form"] = broken["form"][:1]

    with pytest.raises(SubmissionsFormatError, match="form"):
        parse_block(320193, broken, from_date=FROM)


def test_a_missing_column_is_an_error():
    broken = block(AAPL_10K)
    del broken["primaryDocument"]

    with pytest.raises(SubmissionsFormatError, match="primaryDocument"):
        parse_block(320193, broken, from_date=FROM)


def test_a_malformed_filing_date_is_an_error_not_a_dropped_row():
    row = ("0000320193-23-000106", "10-K", "11/03/2023", "2023-09-30", "a.htm")

    with pytest.raises(SubmissionsFormatError, match="filingDate"):
        parse_block(320193, block(row), from_date=FROM)


# --------------------------------------------------------------------------------------
# collect_cik: recent block + older pages
# --------------------------------------------------------------------------------------


def test_collect_cik_requests_the_zero_padded_submissions_url(
    router: respx.MockRouter, fake: FakeTime
):
    route = router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K, AAPL_10Q)))
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert route.call_count == 1
    assert [t.accession for t in result.targets] == [AAPL_10K[0], AAPL_10Q[0]]


def test_collect_cik_reads_older_pages_and_dedupes_overlapping_accessions(
    router: respx.MockRouter, fake: FakeTime
):
    older = ("0000320193-21-000010", "10-K", "2021-10-29", "2021-09-25", "aapl-20210925.htm")
    files = [
        {"name": "CIK0000320193-submissions-001.json", "filingFrom": "2020-01-01",
         "filingTo": "2022-06-30"}
    ]  # fmt: skip
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K, older), files))
    )
    page = router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(200, json=block(older))  # an older page is a bare block
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert page.call_count == 1
    assert sorted(t.accession for t in result.targets) == sorted([AAPL_10K[0], older[0]])


def test_collect_cik_does_not_request_pages_that_end_before_the_from_date(
    router: respx.MockRouter, fake: FakeTime
):
    files = [
        {"name": "CIK0000320193-submissions-001.json", "filingFrom": "2010-01-01",
         "filingTo": "2019-12-31"}
    ]  # fmt: skip
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K), files))
    )
    stale = router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(500)
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert stale.call_count == 0
    assert len(result.targets) == 1


def test_collect_cik_requests_a_page_whose_range_is_unknown(
    router: respx.MockRouter, fake: FakeTime
):
    """No filingTo means we cannot prove the page is out of range, so it is fetched."""
    files = [{"name": "CIK0000320193-submissions-001.json"}]
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K), files))
    )
    page = router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(200, json=block(AAPL_10Q))
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert page.call_count == 1
    assert len(result.targets) == 2


def test_collect_cik_sums_the_skip_counts_across_pages(router: respx.MockRouter, fake: FakeTime):
    no_doc = ("0000320193-22-000001", "10-K", "2022-11-01", "2022-09-24", "")
    files = [{"name": "CIK0000320193-submissions-001.json"}]
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(no_doc), files))
    )
    router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(
            200, json=block(no_doc[:0] + ("0000320193-21-000001",) + no_doc[1:])
        )
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert result.skipped_no_primary_doc == 2


# --------------------------------------------------------------------------------------
# write_targets / summarize
# --------------------------------------------------------------------------------------


def target(cik: int, accession: str, filed: str, form: str = "10-K", report: str | None = None):
    d = date.fromisoformat
    return FilingTarget(
        cik=cik,
        accession=accession,
        form=form,
        filed=d(filed),
        report_date=d(report) if report else None,
        primary_doc="x.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/x.htm",
    )


def test_write_targets_sorts_by_cik_then_filed_then_accession_and_is_byte_stable(tmp_path: Path):
    items = [
        target(2, "0000000002-23-000001", "2023-02-01", report="2022-12-31"),
        target(1, "0000000001-23-000002", "2023-03-01"),
        target(1, "0000000001-23-000001", "2023-03-01"),
        target(1, "0000000001-22-000009", "2022-03-01"),
    ]
    out = tmp_path / "targets" / "filings.csv"

    write_targets(out, items)
    first = out.read_bytes()
    write_targets(out, list(reversed(items)))

    rows = read_rows(out)
    assert rows[0] == HEADER
    assert [r[1] for r in rows[1:]] == [
        "0000000001-22-000009",
        "0000000001-23-000001",
        "0000000001-23-000002",
        "0000000002-23-000001",
    ]
    assert out.read_bytes() == first
    assert rows[-1][4] == "2022-12-31"
    assert rows[1][4] == ""  # a missing report_date is an empty cell, never the text "None"


def test_write_targets_leaves_no_temp_files_behind(tmp_path: Path):
    out = tmp_path / "filings.csv"

    write_targets(out, [target(1, "0000000001-23-000001", "2023-03-01")])

    assert [p.name for p in tmp_path.iterdir()] == ["filings.csv"]


def test_summarize_counts_by_form_and_report_year_falling_back_to_filed_year():
    items = [
        target(1, "a", "2023-02-01", "10-K", report="2022-12-31"),
        target(1, "b", "2023-05-01", "10-Q", report="2023-03-31"),
        target(2, "c", "2023-05-02", "10-Q", report="2023-03-31"),
        target(2, "d", "2024-02-01", "10-K"),  # no report_date: counted under its filed year
    ]

    counts = summarize(items)

    assert counts == {("10-K", 2022): 1, ("10-K", 2024): 1, ("10-Q", 2023): 2}
    text = render_summary(counts, n_ciks=2)
    assert text.splitlines()[-2].split() == ["all", "2", "2", "4"]  # 10-K, 10-Q, total


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------


def run_main(argv: list[str], fake: FakeTime) -> int:
    with make_client(fake) as client:
        return main(argv, client=client)


def test_main_lists_ciks_from_the_tracked_file_and_writes_the_csv(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    tracked = tmp_path / "tracked.csv"
    tracked.write_text("cik,in_sp500\n320193,true\n0000789019,true\n", encoding="utf-8")
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K)))
    )
    router.get("https://data.sec.gov/submissions/CIK0000789019.json").mock(
        return_value=httpx.Response(
            200,
            json=submissions(
                block(("0000950170-23-035122", "10-K", "2023-08-24", "2023-06-30", "msft.htm"))
            ),
        )
    )
    out = tmp_path / "filings.csv"

    rc = run_main(["--tracked", str(tracked), "--dest", str(out)], fake)

    assert rc == 0
    assert [r[0] for r in read_rows(out)[1:]] == ["320193", "789019"]
    assert "10-K" in capsys.readouterr().out


def test_main_accepts_explicit_ciks_and_ignores_the_tracked_default(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K)))
    )
    out = tmp_path / "filings.csv"

    rc = run_main(["--cik", "320193", "--dest", str(out)], fake)

    assert rc == 0
    assert len(read_rows(out)) == 2


def test_main_writes_nothing_and_fails_when_a_cik_cannot_be_listed(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K)))
    )
    router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
        return_value=httpx.Response(404)
    )
    out = tmp_path / "filings.csv"
    out.write_text("previous good run\n", encoding="utf-8")

    rc = run_main(["--cik", "320193", "--cik", "1", "--dest", str(out)], fake)

    assert rc == 1
    assert out.read_text(encoding="utf-8") == "previous good run\n"
    assert "CIK 1 could not be listed" in caplog.text


def test_main_keep_going_writes_what_it_has_and_reports_the_failed_ciks(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K)))
    )
    router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
        return_value=httpx.Response(404)
    )
    out = tmp_path / "filings.csv"

    rc = run_main(["--cik", "320193", "--cik", "1", "--keep-going", "--dest", str(out)], fake)

    assert rc == 0
    assert [r[0] for r in read_rows(out)[1:]] == ["320193"]
    assert "CIK 1 could not be listed" in caplog.text


def test_main_without_any_ciks_is_an_error(fake: FakeTime, tmp_path: Path):
    rc = run_main(
        ["--tracked", str(tmp_path / "missing.csv"), "--dest", str(tmp_path / "o.csv")], fake
    )

    assert rc == 2


def test_main_rejects_a_from_date_after_the_to_date(fake: FakeTime, tmp_path: Path):
    argv = ["--cik", "1", "--from-date", "2024-01-01", "--to-date", "2023-01-01"]

    assert run_main([*argv, "--dest", str(tmp_path / "o.csv")], fake) == 2


def test_collect_cik_does_not_skip_a_late_filed_page_when_to_date_is_set(
    router: respx.MockRouter, fake: FakeTime
):
    """A report dated inside the window can be filed after to_date, on a page starting later."""
    late = ("0000320193-21-000010", "10-K", "2021-10-29", "2021-09-25", "aapl-20210925.htm")
    files = [{"name": "CIK0000320193-submissions-001.json", "filingFrom": "2021-10-05",
              "filingTo": "2022-06-30"}]  # fmt: skip
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K), files))
    )
    page = router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(200, json=block(late))
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM, to_date=date(2021, 9, 30))

    assert page.call_count == 1
    assert [t.accession for t in result.targets] == [late[0]]


def test_a_files_entry_without_a_name_is_a_format_error_not_a_crash(
    router: respx.MockRouter, fake: FakeTime
):
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(
            200, json=submissions(block(AAPL_10K), [{"filingFrom": "2020-01-01"}])
        )
    )

    with make_client(fake) as client, pytest.raises(SubmissionsFormatError, match="no name"):
        collect_cik(client, 320193, from_date=FROM)


def test_main_keeps_going_past_a_malformed_files_entry(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
        return_value=httpx.Response(200, json=submissions(block(AAPL_10K), [{"x": 1}]))
    )

    rc = run_main(["--cik", "1", "--dest", str(tmp_path / "o.csv")], fake)

    assert rc == 1
    assert not (tmp_path / "o.csv").exists()


def test_a_filing_missing_its_primary_doc_in_two_blocks_is_counted_once(
    router: respx.MockRouter, fake: FakeTime
):
    no_doc = ("0000320193-22-000001", "10-K", "2022-11-01", "2022-09-24", "")
    files = [{"name": "CIK0000320193-submissions-001.json"}]
    router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=submissions(block(no_doc), files))
    )
    router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
        return_value=httpx.Response(200, json=block(no_doc))
    )

    with make_client(fake) as client:
        result = collect_cik(client, 320193, from_date=FROM)

    assert result.skipped_no_primary_doc == 1


def test_written_csv_is_world_readable(tmp_path: Path):
    out = tmp_path / "filings.csv"

    write_targets(out, [target(1, "0000000001-23-000001", "2023-03-01")])

    assert out.stat().st_mode & 0o777 == 0o644
