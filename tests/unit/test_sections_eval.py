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

from eval.sections_eval import (
    DEFAULT_BLAST,
    DEFAULT_CACHE,
    RESAMPLES,
    _blast_sources,
    bootstrap_ci,
    cluster_bootstrap_ci,
    evaluate,
    load_gold,
    main,
    normalize_start,
    render,
    skip_leading_headings,
)

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


# --- the blast-radius cache as a second source, and the report against the target ----------------

CACHE_ACC = "0000000002-23-000002"


@pytest.fixture
def cache(tmp_path: Path) -> tuple[Path, Path]:
    """A blast-radius style cache: `<ticker>_<accession>.htm.gz` plus the JSON that describes it."""
    root = tmp_path / "blast"
    root.mkdir()
    (root / f"ACME_{CACHE_ACC}.htm.gz").write_bytes(gzip.compress(HTML.encode()))
    blast = tmp_path / "blast.json"
    blast.write_text(
        json.dumps(
            {
                "filings": [
                    {
                        "accession": CACHE_ACC,
                        "ticker": "ACME",
                        "cik": 2,
                        "url": "https://www.sec.gov/Archives/edgar/data/2/x/a.htm",
                    }  # fmt: skip
                ]
            }
        )
    )
    return root, blast


def test_a_filing_only_in_the_blast_cache_is_found(store: Path, cache: tuple[Path, Path]):
    rows = [
        {
            "accession": CACHE_ACC,
            "section": "1A",
            "labeled_by": "t",
            "first_sentence": (
                "Our results depend on demand for widgets and on supply chain stability."
            ),
        }  # fmt: skip
    ]

    without = evaluate(rows, store)
    with_cache = evaluate(rows, store, cache_dir=cache[0], blast=cache[1])

    assert without["failures"][0]["reason"] == "filing not in the store"
    assert with_cache["correct"] == 1 and with_cache["failures"] == []


def test_the_fetch_store_wins_over_the_cache_for_the_same_accession(
    store: Path, tmp_path: Path, cache: tuple[Path, Path]
):
    blast = json.loads(cache[1].read_text())
    blast["filings"][0]["accession"] = ACC  # the same accession as the store's filing
    cache[1].write_text(json.dumps(blast))
    (cache[0] / f"ACME_{ACC}.htm.gz").write_bytes(
        gzip.compress(b"<html><body>no items</body></html>")
    )

    result = evaluate(
        load_gold(gold(tmp_path, [row("1", "Acme designs widgets for industrial customers.")])),
        store,
        cache_dir=cache[0],
        blast=cache[1],
    )

    assert result["correct"] == 1


def test_the_report_gives_percent_per_item_and_overall_against_the_90_percent_target(
    store: Path, tmp_path: Path
):
    g = gold(
        tmp_path,
        [
            row("1", "Acme designs widgets for industrial customers around the world."),
            row("1A", "Our results depend on demand for widgets and on supply chain stability."),
            row("7", "Revenue increased because of higher volume and pricing across all regions."),
            row("8", "This is not what the filing says at all."),
        ],
    )

    text = render(evaluate(load_gold(g), store))

    assert "3/4 = 75.0% (target >= 90%: FAIL)" in text
    assert "item   1:  1/1  = 100.0%  [100.0, 100.0]  PASS" in text
    assert "item   8:  0/1  =   0.0%  [  0.0,   0.0]  FAIL" in text
    assert "runbook asks for 100" in text and "n=50" in text


def test_meeting_the_target_is_reported_as_pass_and_recorded_in_the_result(
    store: Path, tmp_path: Path
):
    g = gold(
        tmp_path, [row("1", "Acme designs widgets for industrial customers around the world.")]
    )

    result = evaluate(load_gold(g), store)

    assert result["meets_target"] is True and result["target"] == 0.9
    assert result["accuracy_by_section"] == {"1": 1.0}
    assert "target >= 90%: PASS" in render(result)


def test_every_filing_of_the_committed_gold_template_resolves_to_a_file_the_parser_can_read():
    """Guards the wiring: without it all 50 labels would score `filing not in the store`."""
    repo = Path(__file__).resolve().parents[2]
    template = repo / "eval/sets/drafts/sections_gold_template.jsonl"
    if not (repo / DEFAULT_CACHE).is_dir() or not template.is_file():
        pytest.skip("NOT MEASURED: the blast-radius cache or the template is not on this machine")
    accessions = {json.loads(ln)["accession"] for ln in template.read_text().splitlines() if ln}

    sources = _blast_sources(repo / DEFAULT_CACHE, repo / DEFAULT_BLAST)

    assert accessions <= set(sources), sorted(accessions - set(sources))


# --- sub-headings: the harness skips short lines, so the evaluator skips them too ---


def test_leading_short_lines_are_skipped_like_the_labeling_harness_skips_them():
    text = "Company Background\nThe Company designs, manufactures and markets smartphones."

    assert (
        skip_leading_headings(text) == "The Company designs, manufactures and markets smartphones."
    )


def test_a_chunk_of_short_lines_only_is_left_alone():
    assert skip_leading_headings("Overview\nGeneral") == "Overview\nGeneral"


def test_a_long_first_line_is_untouched():
    text = "The Company designs, manufactures and markets smartphones.\nShort."

    assert skip_leading_headings(text) == text


HTML_SUBHEADED = (
    "<html><body>"
    "<div>Item 1. Business</div>"
    "<div>Company Background</div>"
    "<p>Acme designs widgets for industrial customers around the world. More follows here.</p>"
    "<div>Item 1A. Risk Factors</div>"
    "<p>Our results depend on demand for widgets and on supply chain stability. More.</p>"
    "<div>Item 7. Management's Discussion</div>"
    "<p>Revenue increased because of higher volume and pricing across all regions.</p>"
    "<div>Item 8. Financial Statements</div>"
    "<p>The consolidated financial statements are included in this report below.</p>"
    "<div>SIGNATURES</div></body></html>"
)


def test_a_correctly_placed_boundary_under_a_sub_heading_scores_correct(tmp_path: Path):
    root = tmp_path / "filings"
    (root / "1").mkdir(parents=True)
    (root / "1" / f"{ACC}.htm.gz").write_bytes(gzip.compress(HTML_SUBHEADED.encode()))
    (root / "_manifest.jsonl").write_text(
        json.dumps(
            {
                "accession": ACC,
                "cik": 1,
                "form": "10-K",
                "filed": "2023-11-03",
                "report_date": "2023-09-30",
                "url": "https://www.sec.gov/x/a.htm",
                "s3_uri": None,
                "bytes": 1,
                "sha256": "x",
                "http_status": 200,
                "fetched_at": "2026-11-18T00:00:00Z",
            }  # fmt: skip
        )
        + "\n"
    )
    g = gold(
        tmp_path, [row("1", "Acme designs widgets for industrial customers around the world.")]
    )

    result = evaluate(load_gold(g), root)

    assert result["correct"] == 1, result["failures"]


def test_a_start_that_really_differs_still_fails_after_skipping_sub_headings(
    store: Path, tmp_path: Path
):
    g = gold(
        tmp_path, [row("1", "Some other sentence that is not where the section starts at all.")]
    )

    result = evaluate(load_gold(g), store)

    assert result["correct"] == 0 and result["failures"][0]["reason"] == "start differs"


# --- 95% bootstrap CIs (.claude/rules/eval.md: n, CI, 10,000 resamples) ---------------------------


def test_the_bootstrap_uses_10000_resamples_and_is_deterministic():
    outcomes = [1] * 40 + [0] * 10

    assert RESAMPLES == 10_000
    assert bootstrap_ci(outcomes) == bootstrap_ci(outcomes)


def test_the_label_ci_brackets_the_point_estimate_and_has_the_textbook_width():
    outcomes = [1] * 40 + [0] * 10  # 80% on n=50

    lo, hi = bootstrap_ci(outcomes)

    assert lo < 0.8 < hi
    assert 0.66 <= lo <= 0.70 and 0.89 <= hi <= 0.93  # about 0.8 +- 1.96*sqrt(.8*.2/50) = 0.11


def test_all_correct_gives_a_degenerate_interval_not_an_error():
    assert bootstrap_ci([1] * 10) == (1.0, 1.0)


def test_a_bootstrap_of_nothing_is_an_error_not_a_number():
    with pytest.raises(ValueError, match="at least one"):
        bootstrap_ci([])
    with pytest.raises(ValueError, match="at least one"):
        cluster_bootstrap_ci([])


def test_resampling_whole_filings_is_wider_when_labels_in_a_filing_move_together():
    """5 filings all right and 5 all wrong: 50% overall, with 5 dependent labels per filing."""
    filings = [[1] * 5] * 5 + [[0] * 5] * 5
    labels = [o for f in filings for o in f]

    row_lo, row_hi = bootstrap_ci(labels)
    f_lo, f_hi = cluster_bootstrap_ci(filings)

    assert (f_hi - f_lo) > (row_hi - row_lo)
    assert f_lo < 0.5 < f_hi


def test_the_result_carries_n_filings_and_both_intervals_and_the_report_prints_them(
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
    text = render(result)

    assert result["n_filings"] == 1 and result["resamples"] == 10_000
    assert result["ci95"] == (1.0, 1.0) and result["ci95_by_filing"] == (1.0, 1.0)
    assert "95% bootstrap CI (10,000 resamples)" in text and "1 filings" in text


def test_a_low_lower_bound_is_called_out_even_when_the_point_estimate_passes(
    store: Path, tmp_path: Path
):
    result = {
        "target": 0.9, "meets_target": True, "correct": 46, "n": 50, "accuracy": 0.92,
        "ci95": (0.84, 0.98), "ci95_by_filing": (0.78, 0.99), "resamples": 10_000,
        "n_filings": 10, "runbook_labels": 100,
        "by_section": {"1": {"n": 10, "correct": 9}},
        "accuracy_by_section": {"1": 0.9}, "ci95_by_section": {"1": (0.7, 1.0)},
    }  # fmt: skip

    text = render(result)

    assert "target >= 90%: PASS" in text
    assert "cannot show >= 90% with confidence" in text
