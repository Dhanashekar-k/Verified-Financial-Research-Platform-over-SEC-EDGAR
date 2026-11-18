"""Tests for ``eval/sections_eval.py``: section-start accuracy against the owner's gold labels.

The gold file is the owner's (`eval/sets/sections_gold.jsonl`, protected by a hook); these tests
use a small SYNTHETIC gold file and store under tmp_path. They never create or edit the real one.

Gold row: {"accession", "section", "first_sentence", "labeled_by"} (section is a code like "1A").
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from eval.sections_eval import evaluate, load_gold, main, normalize_start

HTML = (
    "<html><body>"
    "<div>Item 1. Business</div>"
    "<p>Acme designs widgets for industrial customers around the world. More follows here.</p>"
    "<div>Item 1A. Risk Factors</div>"
    "<p>Our results depend on demand for widgets and on supply chain stability. More.</p>"
    "<div>Item 7. Management's Discussion</div>"
    "<p>Revenue increased because of higher volume and pricing across all regions.</p>"
    "<div>Item 8. Financial Statements</div>"
    "<p>The consolidated financial statements are included in this report below.</p>"
    "<div>SIGNATURES</div></body></html>"
)
ACC = "0000000001-23-000001"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "filings"
    (root / "1").mkdir(parents=True)
    (root / "1" / f"{ACC}.htm.gz").write_bytes(gzip.compress(HTML.encode()))
    row = {
        "accession": ACC, "cik": 1, "form": "10-K", "filed": "2023-11-03",
        "report_date": "2023-09-30",
        "url": "https://www.sec.gov/Archives/edgar/data/1/x/a.htm", "s3_uri": None, "bytes": 1,
        "sha256": "x", "http_status": 200, "fetched_at": "2026-11-18T00:00:00Z",
    }  # fmt: skip
    (root / "_manifest.jsonl").write_text(json.dumps(row) + "\n")
    return root


def gold(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "gold.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def row(section: str, sentence: str) -> dict[str, str]:
    return {"accession": ACC, "section": section, "first_sentence": sentence, "labeled_by": "t"}


def test_normalisation_ignores_case_whitespace_quotes_and_punctuation():
    assert normalize_start("  Our  “Results” depend, on DEMAND. ") == "our results depend on demand"


def test_a_section_whose_first_text_matches_the_gold_sentence_is_correct(
    store: Path, tmp_path: Path
):
    g = gold(
        tmp_path,
        [
            row("1", "Acme designs widgets for industrial customers around the world."),
            row("1A", "Our results depend on demand for widgets and on supply chain stability."),
        ],
    )

    result = evaluate(load_gold(g), store)

    assert result["n"] == 2 and result["correct"] == 2 and result["accuracy"] == 1.0
    assert result["by_section"]["1A"] == {"n": 1, "correct": 1}


def test_a_wrong_start_is_a_failure_listed_with_what_the_parser_found(store: Path, tmp_path: Path):
    g = gold(tmp_path, [row("7", "Net sales were a record this year, said the CEO.")])

    result = evaluate(load_gold(g), store)

    assert result["correct"] == 0
    [failure] = result["failures"]
    assert failure["reason"] == "start differs"
    assert failure["parser_start"].startswith("Revenue increased")


def test_a_section_the_parser_did_not_find_is_a_failure(store: Path, tmp_path: Path):
    g = gold(tmp_path, [row("7A", "We are exposed to interest rate risk.")])

    [failure] = evaluate(load_gold(g), store)["failures"]

    assert failure["reason"] == "section not found"


def test_a_filing_that_was_never_fetched_is_a_failure_not_a_crash(store: Path, tmp_path: Path):
    bad = {
        "accession": "0000000009-23-000001",
        "section": "1",
        "first_sentence": "x",
        "labeled_by": "t",
    }

    [failure] = evaluate(load_gold(gold(tmp_path, [bad])), store)["failures"]

    assert failure["reason"] == "filing not in the store"


def test_only_the_first_n_characters_are_compared_so_a_long_gold_sentence_still_matches(
    store: Path, tmp_path: Path
):
    long_gold = (
        "Acme designs widgets for industrial customers around the world, "
        "and it has done so for decades."
    )
    g = gold(tmp_path, [row("1", long_gold)])

    assert evaluate(load_gold(g), store, prefix_chars=40)["correct"] == 1
    assert evaluate(load_gold(g), store, prefix_chars=200)["correct"] == 0


def test_a_malformed_gold_row_is_an_error_naming_the_line(tmp_path: Path):
    g = tmp_path / "gold.jsonl"
    g.write_text('{"accession": "a", "section": "1"}\n')

    with pytest.raises(ValueError, match="line 1"):
        load_gold(g)


def test_main_without_a_gold_file_says_not_measured_and_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(
        [
            "--gold",
            str(tmp_path / "missing.jsonl"),
            "--store",
            str(tmp_path),
            "--out",
            str(tmp_path / "o.json"),
        ]
    )

    assert rc == 0
    assert "NOT MEASURED" in capsys.readouterr().out
    assert not (tmp_path / "o.json").exists()


def test_main_writes_a_result_file_and_fails_below_the_threshold(store: Path, tmp_path: Path):
    g = gold(tmp_path, [row("1", "Acme designs widgets"), row("7", "Totally different words here")])
    out = tmp_path / "result.json"

    rc = main(["--gold", str(g), "--store", str(store), "--out", str(out), "--min-accuracy", "0.9"])

    assert rc == 1
    assert json.loads(out.read_text())["accuracy"] == 0.5


@pytest.mark.parametrize("blank", ["", "   ", "…", "\u00a0"])
def test_a_blank_gold_first_sentence_is_rejected_not_matched_against_everything(
    tmp_path: Path, blank: str
):
    g = tmp_path / "gold.jsonl"
    g.write_text(json.dumps({"accession": ACC, "section": "1", "first_sentence": blank}) + "\n")

    with pytest.raises(ValueError, match="line 1.*blank"):
        load_gold(g)
