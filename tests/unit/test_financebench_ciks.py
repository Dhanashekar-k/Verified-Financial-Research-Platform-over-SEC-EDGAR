"""Tests for ``eval/financebench_ciks.py``: a CIK is accepted only when the SEC's submissions show
the exact 10-K / 10-Q fiscal years FinanceBench cites. HTTP is respx; SEC calls use SecClient.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eval.financebench_ciks import candidates, main, normalize, parse_doc, resolve, verify
from pipelines.fetch.list_filings import FilingTarget
from pipelines.fetch.sec_client import SecClient, TokenBucket
from tests.fake_time import FakeTime

NAMES = [
    (111, "ACME WIDGETS CORP", "ACME"),
    (222, "ACME WIDGETS FINANCE CREDIT CORP", ""),
    (333, "BIG RIVER HOLDINGS INC", "BRH"),
    (444, "ZETA CO", "ZETA"),
]


def filing(form: str, report: str, acc: str = "0000000001-23-000001") -> FilingTarget:
    return FilingTarget(
        cik=1, accession=acc, form=form, filed=date.fromisoformat(report),
        report_date=date.fromisoformat(report),
        primary_doc="a.htm", url="https://www.sec.gov/Archives/edgar/data/1/x/a.htm",
    )  # fmt: skip


def test_normalisation_drops_legal_suffixes_punctuation_and_case():
    assert normalize("COSTCO WHOLESALE CORP /NEW") == "costco wholesale"
    assert normalize("Johnson & Johnson") == "johnson and johnson"
    assert normalize("Block, Inc.") == "block"


def test_an_exact_name_or_ticker_scores_100_and_a_word_prefix_scores_95():
    got = {c.cik: c.score for c in candidates("Acme Widgets", NAMES)}

    assert got == {111: 100.0, 222: 95.0}
    assert candidates("ZETA", NAMES)[0].cik == 444  # the ticker
    assert candidates("Totally Unrelated Name", NAMES) == []


def test_a_prefix_must_end_at_a_word_boundary():
    assert candidates("Big Riv", NAMES) == []


def test_documents_are_parsed_into_year_and_kind():
    assert parse_doc("3M_2023Q2_10Q") == {"year": 2023, "kind": "10Q"}
    assert parse_doc("AMCOR_2022_8K_dated-2022-07-01") == {"year": 2022, "kind": "8K"}
    assert parse_doc("PFIZER_2021_10K") == {"year": 2021, "kind": "10K"}
    assert parse_doc("nonsense") is None


def test_verify_matches_each_cited_year_and_form_and_reports_what_is_missing():
    filings = [filing("10-K", "2018-12-31", "A"), filing("10-Q", "2023-06-30", "B")]

    v = verify(filings, ["X_2018_10K", "X_2023Q2_10Q", "X_2022_10K", "X_2022_8K_dated-2022-05-20"])

    assert v["matched"] == {"X_2018_10K": "A", "X_2023Q2_10Q": "B"}
    assert v["missing"] == ["X_2022_10K"] and v["unchecked"] == ["X_2022_8K_dated-2022-05-20"]
    assert v["ok"] is False
    assert verify(filings, ["X_2018_10K"])["ok"] is True
    assert verify(filings, ["X_2022_8K_dated-2022-05-20"])["ok"] is False  # nothing checkable


def sub_json(name: str, *rows: tuple[str, str, str]) -> dict[str, Any]:
    """A submissions document whose filings report on the given dates: (accession, form, date)."""
    return {
        "name": name,
        "filings": {
            "recent": {
                "accessionNumber": [r[0] for r in rows],
                "form": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "reportDate": [r[2] for r in rows],
                "primaryDocument": ["a.htm" for _ in rows],
            },
            "files": [],
        },
    }


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`main()` calls load_dotenv: never load the developer's .env into this process."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


def client() -> SecClient:
    fake = FakeTime()
    bucket = TokenBucket(rate=8, clock=fake.clock, sleep=fake.bucket_sleep)
    return SecClient(
        user_agent="test-agent test@example.invalid", bucket=bucket, sleep=fake.backoff_sleep,
        rng=random.Random(1),
    )  # fmt: skip


def mock_cik(router: respx.MockRouter, cik: int, name: str, *rows: tuple[str, str, str]) -> None:
    router.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").mock(
        return_value=httpx.Response(200, json=sub_json(name, *rows))
    )


def test_the_candidate_whose_filings_match_every_cited_year_is_chosen(router: respx.MockRouter):
    mock_cik(
        router, 111, "ACME WIDGETS CORP", ("A", "10-K", "2018-12-31"), ("B", "10-K", "2022-12-31")
    )
    mock_cik(router, 222, "ACME WIDGETS FINANCE CREDIT CORP", ("C", "10-K", "2022-12-31"))

    with client() as c:
        out = resolve(c, "Acme Widgets", ["ACME_2018_10K", "ACME_2022_10K"], NAMES)

    assert (out["status"], out["cik"]) == ("resolved", 111)
    assert [t["ok"] for t in out["candidates_tried"]] == [True, False]  # the credit arm lacks 2018


def test_two_equally_good_verified_candidates_are_ambiguous_and_nothing_is_chosen(
    router: respx.MockRouter,
):
    names = [(1, "TWIN CORP", ""), (2, "TWIN INC", "")]
    for cik in (1, 2):
        mock_cik(router, cik, "TWIN", ("A", "10-K", "2020-12-31"))

    with client() as c:
        out = resolve(c, "Twin", ["TWIN_2020_10K"], names)

    assert (out["status"], out["cik"]) == ("ambiguous", None)


def test_a_company_with_no_matching_name_is_unresolved():
    with client() as c:
        out = resolve(c, "Nobody Heard Of", ["NOBODY_2020_10K"], NAMES)

    assert (out["status"], out["cik"]) == ("unresolved", None)


def test_a_name_that_matches_but_whose_filings_do_not_is_not_accepted(router: respx.MockRouter):
    mock_cik(router, 444, "ZETA CO", ("A", "10-K", "2015-12-31"))

    with client() as c:
        out = resolve(c, "ZETA", ["ZETA_2020_10K"], NAMES)

    assert out["status"] == "unresolved"


def test_a_company_citing_only_8ks_is_accepted_on_an_exact_unique_name_and_flagged(
    router: respx.MockRouter,
):
    mock_cik(router, 444, "ZETA CO", ("A", "10-K", "2021-12-31"))

    with client() as c:
        out = resolve(c, "ZETA", ["ZETA_2022_8K_dated-2022-05-20"], NAMES)

    assert (out["status"], out["cik"]) == ("resolved_name_only", 444)


def test_a_prefix_only_match_is_never_accepted_on_name_alone(router: respx.MockRouter):
    mock_cik(router, 111, "ACME WIDGETS CORP", ("A", "10-K", "2021-12-31"))
    mock_cik(router, 222, "ACME WIDGETS FINANCE CREDIT CORP", ("A", "10-K", "2021-12-31"))
    names = [(222, "ACME WIDGETS FINANCE CREDIT CORP", "")]

    with client() as c:
        out = resolve(c, "Acme Widgets", ["ACME_2022_8K_dated-2022-05-20"], names)

    assert out["status"] == "unresolved"  # score 95, not an exact match


def test_main_writes_only_resolved_companies_and_the_evidence(
    router: respx.MockRouter, tmp_path: Path
):
    fb = tmp_path / "fb"
    fb.mkdir()
    rows = [
        {"company": "ZETA", "doc_name": "ZETA_2020_10K"},
        {"company": "Nobody", "doc_name": "NOBODY_2020_10K"},
    ]
    (fb / "financebench_open_source.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    tickers = tmp_path / "t.json"
    tickers.write_text(json.dumps({"0": {"cik_str": 444, "ticker": "ZETA", "title": "ZETA CO"}}))
    mock_cik(router, 444, "ZETA CO", ("A", "10-K", "2020-12-31"))
    out, evidence = tmp_path / "c.csv", tmp_path / "e.json"

    with client() as c:
        rc = main(
            ["--fb", str(fb), "--tickers", str(tickers), "--lake-names", str(tmp_path / "none.csv"),
             "--out", str(out), "--evidence", str(evidence)],
            client=c,
        )  # fmt: skip

    assert rc == 1  # one company is unresolved
    assert list(csv.reader(out.open())) == [["company", "cik"], ["ZETA", "444"]]
    saved = json.loads(evidence.read_text())
    assert (saved["n_companies"], saved["n_resolved"]) == (2, 1)
    assert {r["company"]: r["status"] for r in saved["results"]} == {
        "Nobody": "unresolved",
        "ZETA": "resolved",
    }
