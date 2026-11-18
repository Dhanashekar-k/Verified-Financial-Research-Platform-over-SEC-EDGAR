"""Tests for ``eval/financebench_recheck.py``: confirming FinanceBench companies from the cover
pages of the cited documents against the SEC submissions (renames, reincorporations, 8-Ks).
Covers are synthetic; HTTP is respx through SecClient; PDFs are built in memory.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest
import respx

from eval.financebench_recheck import (
    check_document,
    main,
    names_in_force,
    read_cover,
    recheck_company,
    same_file_number,
)
from pipelines.fetch.sec_client import SecClient, TokenBucket
from tests.fake_time import FakeTime

TENK = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-K ANNUAL REPORT For the fiscal year "
    "ended December 31, 2016 OR TRANSITION REPORT For the transition period from to "
    "Commission File "
    "Number 001-37622 SQUARE, INC. (Exact name of registrant as specified in its charter) Delaware"
)
TENQ = (
    "FORM 10-Q QUARTERLY REPORT For the Quarterly Period Ended December 31, 2022 Commission File "
    "Number 001-38932 AMCOR PLC (Exact name of Registrant as specified in its charter) Jersey"
)
EIGHTK = (
    "FORM 8-K CURRENT REPORT Date of report (Date of earliest event reported): May 20, 2022 "
    "(May 18, 2022) Foot Locker, Inc. (Exact name of registrant as specified in charter) New York "
    "1-10299 13-3513936 (State or other jurisdiction of incorporation)"
)
RENAMED = {
    "name": "Block, Inc.",
    "formerNames": [
        {
            "name": "Square, Inc.",
            "from": "2011-03-02T00:00:00.000Z",
            "to": "2021-12-08T00:00:00.000Z",
        }
    ],
}  # fmt: skip


def test_the_cover_of_a_10k_gives_the_name_without_the_text_in_front_of_it():
    cover = read_cover(TENK)

    assert cover == {
        "registrant": "SQUARE, INC.", "file_number": "001-37622",
        "period_end": "2016-12-31", "date_of_report": None,
    }  # fmt: skip


def test_the_covers_of_a_10q_and_an_8k_are_read():
    q, k8 = read_cover(TENQ), read_cover(EIGHTK)

    assert (q["registrant"], q["period_end"], q["file_number"]) == (
        "AMCOR PLC",
        "2022-12-31",
        "001-38932",
    )
    assert (k8["registrant"], k8["date_of_report"], k8["file_number"]) == (
        "Foot Locker, Inc.", "2022-05-20", "1-10299",
    )  # fmt: skip


def test_an_unreadable_cover_gives_none_not_a_guess():
    assert read_cover("a page of ordinary prose with no cover") == {
        "registrant": None, "file_number": None, "period_end": None, "date_of_report": None,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("1-10299", "001-10299", True),
        ("001-36786", "001-38932", False),
        (None, "001-1", False),
        ("001-5", None, False),
    ],
)
def test_file_numbers_compare_without_leading_zeros(a, b, same):
    assert same_file_number(a, b) is same


def test_the_name_in_force_follows_the_rename_date():
    assert names_in_force(RENAMED, "2017-02-27") == {"square"}
    assert names_in_force(RENAMED, "2021-12-08") == {"square"}
    assert names_in_force(RENAMED, "2022-02-23") == {"block"}
    assert names_in_force({"name": "Amcor plc", "formerNames": []}, "2020-08-27") == {"amcor"}


def rows(*r: tuple[str, str, str, str, str]) -> list[dict[str, str]]:
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "fileNumber")
    return [{**dict(zip(keys, x, strict=True)), "primaryDocument": "a.htm"} for x in r]


def test_a_renamed_company_is_confirmed_by_the_name_it_used_on_the_filing_date():
    filings = rows(("A", "10-K", "2017-02-27", "2016-12-31", "001-37622"))

    out = check_document("BLOCK_2016_10K", read_cover(TENK), RENAMED, filings)

    assert out["status"] == "confirmed"
    assert out["checks"] == {
        "filing_found_by_exact_date": True,
        "registrant_is_the_name_in_force_on_the_filing_date": True,
        "file_number_matches_sec_metadata": True,
    }


def test_a_cover_that_uses_the_new_name_on_an_old_filing_is_a_mismatch():
    cover = {**read_cover(TENK), "registrant": "BLOCK, INC."}
    filings = rows(("A", "10-K", "2017-02-27", "2016-12-31", "001-37622"))

    assert check_document("BLOCK_2016_10K", cover, RENAMED, filings)["status"] == "mismatch"


def test_a_year_is_not_enough_the_exact_period_end_must_exist():
    filings = rows(("A", "10-K", "2017-02-27", "2016-06-30", "001-37622"))

    assert (
        check_document("BLOCK_2016_10K", read_cover(TENK), RENAMED, filings)["status"]
        == "no_matching_filing"
    )


def test_a_different_file_number_is_a_mismatch_until_the_original_filing_explains_it():
    filings = rows(("A", "10-K", "2017-02-27", "2016-12-31", "001-99999"))

    out = check_document("BLOCK_2016_10K", read_cover(TENK), RENAMED, filings)

    assert (
        out["status"] == "mismatch" and out["checks"]["file_number_matches_sec_metadata"] is False
    )


def test_an_8k_is_found_by_its_exact_filing_or_report_date_and_an_earnings_release_is_unchecked():
    main_doc = {"name": "FOOT LOCKER, INC.", "formerNames": []}
    filings = rows(("B", "8-K", "2022-05-20", "2022-05-18", "001-10299"))

    k8 = check_document(
        "FOOTLOCKER_2022_8K_dated-2022-05-20", read_cover(EIGHTK), main_doc, filings
    )
    wrong_day = check_document(
        "FOOTLOCKER_2022_8K_dated-2022-05-21",
        {**read_cover(EIGHTK), "date_of_report": "2022-05-21"},
        main_doc,
        filings,
    )
    release = check_document("AMCOR_2023Q4_EARNINGS", read_cover("nothing"), main_doc, filings)

    assert k8["status"] == "confirmed" and k8["sec_filing"]["accessionNumber"] == "B"
    assert wrong_day["status"] == "no_matching_filing"
    assert release["status"] == "unchecked"


# -- against a mocked SEC and real PDFs ----------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


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


def submissions(
    name: str, former: list[dict[str, str]], *filings: tuple[str, str, str, str, str]
) -> dict[str, Any]:
    cols = ("accessionNumber", "form", "filingDate", "reportDate", "fileNumber")
    recent = {c: [f[i] for f in filings] for i, c in enumerate(cols)} | {
        "primaryDocument": ["a.htm"] * len(filings)
    }
    return {"name": name, "formerNames": former, "filings": {"recent": recent, "files": []}}


def make_pdf(path: Path, cover: str) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 560, 800), cover, fontsize=9)
    doc.save(path)


def test_the_original_filings_cover_explains_a_file_number_the_sec_metadata_disagrees_with(
    router: respx.MockRouter, tmp_path: Path
):
    fb = tmp_path / "fb"
    (fb / "pdfs").mkdir(parents=True)
    make_pdf(fb / "pdfs" / "BLOCK_2016_10K.pdf", TENK)
    router.get("https://data.sec.gov/submissions/CIK0000001234.json").mock(
        return_value=httpx.Response(
            200,
            json=submissions(
                "Block, Inc.",
                RENAMED["formerNames"],
                ("0001-17-000001", "10-K", "2017-02-27", "2016-12-31", "001-99999"),
            ),
        )  # fmt: skip
    )
    original = f"<html><body><div>{TENK}</div></body></html>"
    router.get("https://www.sec.gov/Archives/edgar/data/1234/000117000001/a.htm").mock(
        return_value=httpx.Response(200, content=original.encode())
    )

    with client() as c:
        out = recheck_company(c, "Block", 1234, ["BLOCK_2016_10K"], fb)

    [doc] = out["documents"]
    assert doc["status"] == "confirmed_pdf_matches_original_filing"
    assert doc["pdf_cover_equals_original_cover"] == {
        "file_number": True,
        "registrant": True,
        "period_end": True,
    }
    assert out["all_confirmed"] is True


def test_a_pdf_that_is_not_the_filing_stays_a_mismatch(router: respx.MockRouter, tmp_path: Path):
    fb = tmp_path / "fb"
    (fb / "pdfs").mkdir(parents=True)
    make_pdf(fb / "pdfs" / "BLOCK_2016_10K.pdf", TENK)
    router.get("https://data.sec.gov/submissions/CIK0000001234.json").mock(
        return_value=httpx.Response(
            200,
            json=submissions(
                "Block, Inc.",
                RENAMED["formerNames"],
                ("0001-17-000001", "10-K", "2017-02-27", "2016-12-31", "001-99999"),
            ),
        )  # fmt: skip
    )
    other = TENK.replace("001-37622", "001-11111")
    router.get("https://www.sec.gov/Archives/edgar/data/1234/000117000001/a.htm").mock(
        return_value=httpx.Response(200, content=f"<html><body>{other}</body></html>".encode())
    )

    with client() as c:
        out = recheck_company(c, "Block", 1234, ["BLOCK_2016_10K"], fb)

    assert out["documents"][0]["status"] == "mismatch" and out["all_confirmed"] is False


def test_main_writes_the_recheck_into_the_evidence_file_and_lists_the_10ks(
    router: respx.MockRouter, tmp_path: Path
):
    fb = tmp_path / "fb"
    (fb / "pdfs").mkdir(parents=True)
    make_pdf(fb / "pdfs" / "FOOTLOCKER_2022_8K_dated-2022-05-20.pdf", EIGHTK)
    evidence = tmp_path / "e.json"
    evidence.write_text(json.dumps({"n_companies": 1, "results": [
        {"company": "Foot Locker", "cik": 850209, "status": "resolved_name_only",
         "documents": ["FOOTLOCKER_2022_8K_dated-2022-05-20"]}]}))  # fmt: skip
    router.get("https://data.sec.gov/submissions/CIK0000850209.json").mock(
        return_value=httpx.Response(
            200,
            json=submissions(
                "FOOT LOCKER, INC.",
                [],
                ("B", "8-K", "2022-05-20", "2022-05-18", "001-10299"),
                ("K1", "10-K", "2022-03-25", "2022-01-29", "001-10299"),
                ("K2", "10-K", "2023-03-24", "2023-01-28", "001-10299"),
            ),
        )  # fmt: skip
    )

    with client() as c:
        rc = main(
            ["--companies", "Foot Locker", "--fb", str(fb), "--evidence", str(evidence)], client=c
        )

    saved = json.loads(evidence.read_text())
    [res] = saved["recheck"]["results"]
    assert rc == 0 and res["all_confirmed"] is True
    assert res["10k_report_dates"] == ["2022-01-29", "2023-01-28"]
    assert saved["results"][0]["status"] == "resolved_name_only"  # the original result is untouched
