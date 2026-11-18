"""Tests for ``eval/parse_report.py`` (measurements) and ``eval/check_source_urls.py``."""

from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

import httpx
import pymupdf
import pytest
import respx

from eval.check_source_urls import check_urls
from eval.check_source_urls import main as url_main
from eval.parse_report import financebench_metrics, html_metrics, render, token_histogram
from pipelines.fetch.sec_client import SecClient, TokenBucket
from tests.fake_time import FakeTime

NAMES = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike".split()
PARA = "".join(
    f"<p>Sentence {name} says revenue and margins moved because of demand and pricing.</p>"
    for name in NAMES
)  # distinct words: a paragraph repeated 6+ times with only digits changing is a page header
HTML = (
    "<html><body><div>Item 1. Business</div>" + PARA + "<div>Item 1A. Risk Factors</div>" + PARA
    + "<div>Item 7. MD&amp;A</div>" + PARA + "<div>Item 8. Financial Statements</div>" + PARA
    + "<div>SIGNATURES</div></body></html>"
)  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "filings"
    lines = []
    for cik, acc in ((1, "0000000001-23-000001"), (2, "0000000002-23-000001")):
        (root / str(cik)).mkdir(parents=True, exist_ok=True)
        (root / str(cik) / f"{acc}.htm.gz").write_bytes(gzip.compress(HTML.encode()))
        lines.append(
            {
                "accession": acc,
                "cik": cik,
                "form": "10-K",
                "filed": "2023-11-03",
                "report_date": "2023-09-30",
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/x/a.htm",
                "s3_uri": None,
                "bytes": 1,
                "sha256": "x",
                "http_status": 200,
                "fetched_at": "2026-11-18T00:00:00Z",
            }  # fmt: skip
        )
    lines.append({**lines[0], "accession": "0000000003-23-000001", "cik": 3, "http_status": 404})
    (root / "_manifest.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    return root


def test_the_histogram_has_eight_bins_and_counts_every_chunk():
    hist = token_histogram([1, 50, 51, 400, 399, 120])

    assert list(hist) == [
        "1-50",
        "51-100",
        "101-150",
        "151-200",
        "201-250",
        "251-300",
        "301-350",
        "351-400",
    ]
    assert hist["1-50"] == 2 and hist["351-400"] == 2 and sum(hist.values()) == 6


def test_html_metrics_counts_filings_chunks_and_skips_failed_fetches(store: Path):
    m = html_metrics(store, seed=1)

    assert m["n_filings"] == 2  # the 404 row is not parsed
    assert m["status_by_form"] == {"10-K|ok": 2}
    assert m["non_ok"] == [] and m["chunks_over_400"] == 0 and m["max_tokens"] <= 400
    assert sum(m["chunks_per_section"].values()) == m["n_chunks"]
    assert len(m["random_chunks"]) == min(20, m["n_chunks"]) and len(m["url_sample"]) <= 50


def test_the_sample_is_reproducible_for_a_seed_and_differs_for_another(store: Path):
    a, b, c = html_metrics(store, 5), html_metrics(store, 5), html_metrics(store, 6)

    assert a["random_chunks"] == b["random_chunks"]
    assert a["random_chunks"] != c["random_chunks"]


def test_a_failing_filing_is_listed_with_its_cause(store: Path):
    (store / "1" / "0000000001-23-000001.htm.gz").write_bytes(
        gzip.compress(b"<html><body><div>Item 1. Business</div><p>x.</p></body></html>")
    )

    m = html_metrics(store, 1)

    [bad] = m["non_ok"]
    assert (bad["status"], bad["error"]) == ("partial", "missing sections: 1A, 7, 8")


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 780), text, fontsize=9)
    return doc.tobytes()


def test_financebench_metrics_measure_the_evidence_page_convention(tmp_path: Path):
    base = tmp_path / "fb"
    (base / "pdfs").mkdir(parents=True)
    page0 = "The Company reported capital expenditures of 1,577 million dollars for the year."
    (base / "pdfs" / "ACME_2022_10K.pdf").write_bytes(
        make_pdf([page0, "Other page text entirely."])
    )
    q = {"company": "Acme", "doc_name": "ACME_2022_10K",
         "evidence": [{"evidence_text": page0, "evidence_page_num": 0}]}  # fmt: skip
    (base / "financebench_open_source.jsonl").write_text(json.dumps(q) + "\n")

    m = financebench_metrics(base)

    assert m["parse_status"] == {"ok": 1}
    assert m["evidence_page_convention"] == {"evidence_page_num_is_0_based_index": 1}
    assert m["pages_with_text"] == 2


def test_render_quotes_the_measured_numbers_and_says_the_full_run_is_not_measured(store: Path):
    text = render({"html": html_metrics(store, 3)})

    assert "NOT MEASURED" in text and "10-K | ok | 2" in text
    assert "## FinanceBench PDFs" in text and "random chunks" in text


# -- URL check ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`main()` calls load_dotenv: never load the developer's .env into this process."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


def make_client(fake: FakeTime) -> SecClient:
    bucket = TokenBucket(rate=2, clock=fake.clock, sleep=fake.bucket_sleep)
    return SecClient(
        user_agent="test-agent test@example.invalid", bucket=bucket, sleep=fake.backoff_sleep,
        rng=random.Random(1),
    )  # fmt: skip


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


def test_each_document_is_requested_once_with_the_fragment_removed(router: respx.MockRouter):
    a = router.get("https://www.sec.gov/Archives/edgar/data/1/x/a.htm").mock(
        return_value=httpx.Response(200, content=b"<html>")
    )
    b = router.get("https://www.sec.gov/Archives/edgar/data/2/y/b.htm").mock(
        return_value=httpx.Response(404)
    )
    sampled = [
        "https://www.sec.gov/Archives/edgar/data/1/x/a.htm#:~:text=Risk%20Factors",
        "https://www.sec.gov/Archives/edgar/data/1/x/a.htm#i123",
        "https://www.sec.gov/Archives/edgar/data/2/y/b.htm#i9",
    ]

    with make_client(FakeTime()) as client:
        result = check_urls(client, sampled)

    assert (a.call_count, b.call_count) == (1, 1)
    assert (result["sampled_chunk_urls"], result["distinct_documents"], result["ok"]) == (3, 2, 1)
    assert [f["status"] for f in result["failed"]] == [404]


def test_main_reports_not_measured_without_the_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = url_main(["--metrics", str(tmp_path / "none.json"), "--out", str(tmp_path / "o.json")])

    assert rc == 0 and "NOT MEASURED" in capsys.readouterr().out


def test_main_returns_1_when_any_document_fails(router: respx.MockRouter, tmp_path: Path):
    router.get("https://www.sec.gov/Archives/edgar/data/1/x/a.htm").mock(
        return_value=httpx.Response(404)
    )
    metrics = tmp_path / "m.json"
    metrics.write_text(
        json.dumps(
            {"html": {"url_sample": ["https://www.sec.gov/Archives/edgar/data/1/x/a.htm#i1"]}}
        )
    )

    with make_client(FakeTime()) as client:
        rc = url_main(["--metrics", str(metrics), "--out", str(tmp_path / "o.json")], client=client)

    assert rc == 1
    assert json.loads((tmp_path / "o.json").read_text())["ok"] == 0
