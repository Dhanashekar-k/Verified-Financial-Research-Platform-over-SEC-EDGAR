"""Unit tests for the DQ report renderer and the NOT MEASURED mechanism (no Spark)."""

from __future__ import annotations

from typing import Any

from pipelines.spark.dq_checks import NotMeasured, measure, pct, quarter_range, render_report


def full_metrics() -> dict[str, Any]:
    return {
        "generated_at": "2026-11-17T00:00:00+00:00",
        "git_sha": "abc1234",
        "snapshots": {"lake.raw.fsds_num": 111, "lake.curated.facts": None},
        "rows_per_quarter": {
            "tables": {
                "sub": {"2023q3": 7, "2024q1": 5},
                "num": {"2023q3": 70, "2024q1": 50},
                "tag": {"2023q3": 3, "2024q1": 3},
                "pre": {"2023q3": 9, "2024q1": 8},
            },
            "first_quarter": "2023q3",
            "last_quarter": "2024q1",
            "missing_quarters": ["2023q4"],
            "starts_at_2009q2": False,
        },
        "raw_num_null_rates": {"rows": 120, "null_rate": {"segments": 0.5, "value": 0.0125}},
        "uom": {
            "rows": 120,
            "distinct_uom": 2,
            "top": [{"uom": "USD", "rows": 100, "share": 100 / 120}],
        },
        "raw_shares": {
            "rows": 120,
            "custom": {"rows": 12, "share": 0.1},
            "segmented": {"rows": 60, "share": 0.5},
            "coreg": {"rows": 0, "share": 0.0},
            "null_value": {"rows": 3, "share": 0.025},
        },
        "fye_changes": {
            "companies": 10,
            "companies_with_changed_fye": 1,
            "examples": [{"cik": 5, "name": "X Corp", "fyes": ["0930", "1231"]}],
        },
        "label_offsets": {"companies": 10, "offsets": {-1: 2, 0: 8}, "non_constant": 1},
        "drop_counts": {
            "rows": 120,
            "null_value": 3,
            "unparseable_ddate": 0,
            "no_matching_sub": 2,
            "no_fye": 6,
            "dropped_any": 9,
        },
        "curated_facts": {
            "rows": 1000,
            "facts": 950,
            "rows_by_fiscal_year": {2023: 600, 2022: 350, None: 50},
            "null_rate": {"calendar_frame": 0.25},
            "custom": {"rows": 100, "share": 0.1},
            "restated_facts": 7,
            "derived_q4": {"rows": 11},
            "with_dq_flags": {"rows": 50, "share": 0.05},
            "flag_counts": {"q4_derived": 11},
            "n_versions_per_fact": {1: 900, 2: 50},
        },
        "spotcheck": {
            "summary": {
                "matched": 58,
                "n_comparisons": 60,
                "outcomes": {"match": 58, "value_mismatch": 2},
                "expected_comparisons": 60,
                "frames_agree": 50,
                "frames_compared": 55,
                "unclassified_non_matches": 2,
            },
            "generated_at": "2026-11-17T01:00:00+00:00",
            "facts_snapshot_at_check": 5,
            "facts_snapshot_now": 5,
            "stale": False,
        },
        "apple_revenue_raw": {
            "rows": [
                {
                    "adsh": "0000320193-23-000106",
                    "tag": "Revenues",
                    "uom": "USD",
                    "value": "383285000000.0000",
                }
            ]
        },
        "apple_revenue_curated": {
            "rows": [
                {"tag": "Revenues", "uom": "USD", "value": "383285000000.0000", "accession": "a"}
            ]
        },
    }


def test_a_fully_measured_report_has_every_number_and_no_not_measured_marker():
    text = render_report(full_metrics())

    assert "NOT MEASURED" not in text.replace("`NOT MEASURED` means", "")
    assert "2023q4" in text and "Missing quarters in range: **['2023q4']**" in text
    assert "| 2023q3 | 7 | 70 | 3 | 9 |" in text
    assert "| `segments` | 50.00% |" in text
    assert "58/60 match" in text
    assert "{-1: 2, 0: 8}" in text
    assert "383285000000.0000" in text
    assert "does not assert a match" in text


def test_unmeasurable_sections_say_not_measured_with_the_reason_and_never_a_number():
    m = full_metrics()
    m["curated_facts"] = NotMeasured("table lake.curated.facts does not exist")
    m["label_offsets"] = NotMeasured("a Shekar-owned function is not implemented yet")
    m["spotcheck"] = NotMeasured("FileNotFoundError: docs/milestones/M01-spotcheck.json")

    text = render_report(m)

    assert "**NOT MEASURED**: table lake.curated.facts does not exist" in text
    assert "**NOT MEASURED**: a Shekar-owned function is not implemented yet" in text
    assert "**NOT MEASURED**: FileNotFoundError" in text
    assert "1,000 rows" not in text  # nothing leaked from the unmeasured section
    assert "58/60" not in text


def test_measure_turns_not_implemented_and_other_errors_into_not_measured():
    def stub() -> None:
        raise NotImplementedError

    def boom() -> None:
        raise RuntimeError("table not found\nsecond line")

    nm = measure(stub)
    assert isinstance(nm, NotMeasured) and "not implemented" in nm.reason
    other = measure(boom)
    assert isinstance(other, NotMeasured)
    assert other.reason == "RuntimeError: table not found"
    assert measure(lambda: {"ok": 1}) == {"ok": 1}


def test_pct_formats_shares_and_handles_none():
    assert pct(0.12345) == "12.35%"
    assert pct(None) == "n/a"
    assert pct(0.0) == "0.00%"


def test_quarter_range_is_inclusive_and_crosses_years():
    assert quarter_range("2023q3", "2024q2") == ["2023q3", "2023q4", "2024q1", "2024q2"]
    assert quarter_range("2023q3", "2023q3") == ["2023q3"]


def test_a_null_fiscal_year_is_reported_not_a_crash():
    """normalize can leave fiscal_year NULL (e.g. a NULL fye); sorting must not blow up."""
    text = render_report(full_metrics())

    assert "NULL: 50" in text
    assert text.index("2022: 350") < text.index("2023: 600") < text.index("NULL: 50")


def test_a_stale_spot_check_is_flagged_and_an_incomplete_one_too():
    m = full_metrics()
    m["spotcheck"]["stale"] = True
    m["spotcheck"]["facts_snapshot_now"] = 9
    m["spotcheck"]["summary"]["n_comparisons"] = 40

    text = render_report(m)

    assert "**STALE**" in text and "snapshot 5 then, 9 now" in text
    assert "**INCOMPLETE**: 40 comparisons were made, 60 were expected" in text


def test_the_report_never_prints_a_unit_it_did_not_measure():
    m = full_metrics()
    m["apple_revenue_curated"] = {
        "rows": [{"tag": "Revenues", "uom": "EUR", "value": "1.0000", "accession": "a"}]
    }

    text = render_report(m)

    assert "= 1.0000 EUR" in text
    assert "(USD)" not in text


def test_drop_counts_section_lists_every_cause():
    text = render_report(full_metrics())

    assert "no `sub` row for the accession | 2 |" in text
    assert "`value` is NULL | 3 |" in text
    assert "`ddate` is not a valid yyyymmdd date | 0 |" in text
    assert "no fiscal year end (`sub.fye` NULL or empty) | 6 |" in text


def test_drop_counts_section_shows_the_distinct_total_and_what_is_kept():
    """The causes overlap, so the section must also give the distinct total: 9 of 120 rows."""
    text = render_report(full_metrics())

    assert "dropped for at least one cause** (each row once) | 9 |" in text
    assert "Of 120 raw rows" in text
