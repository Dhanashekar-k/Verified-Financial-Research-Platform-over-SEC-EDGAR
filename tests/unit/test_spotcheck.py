"""Unit tests for the companyfacts spot check (`pipelines/spark/spotcheck_companyfacts.py`).

No Spark session and no network: companyfacts payloads are synthetic dicts in the real API
shape, curated rows are plain dicts, and the HTTP fetch goes through respx.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from pipelines.fetch.sec_client import SecClient, TokenBucket
from pipelines.spark.spotcheck_companyfacts import (
    DATE_TOLERANCE_DAYS,
    METRICS,
    SPOT_COMPANIES,
    Comparison,
    companyfacts_url,
    compare,
    expected_fact,
    fetch_all,
    latest_annual_filings,
    parse_fiscal_years,
    select_filings,
    summarize,
)

APPLE = 320193
ACC_23 = "0000320193-23-000106"  # FY2023 10-K
ACC_22 = "0000320193-22-000108"  # FY2022 10-K
ACC_Q = "0000320193-23-000077"  # a 10-Q


def entry(accn: str, end: str, val: int, start: str | None = None, form: str = "10-K", **kw: Any):
    e: dict[str, Any] = {
        "end": end,
        "val": val,
        "accn": accn,
        "fy": 2022 if accn == ACC_22 else 2023,  # the filing's own fiscal-year label
        "fp": "FY",
        "form": form,
        "filed": "2023-11-03"
        if accn == ACC_23
        else "2022-10-28"
        if accn == ACC_22
        else "2023-08-04",
    }
    if start:
        e["start"] = start
    return e | kw


def apple_cf() -> dict[str, Any]:
    rev = "RevenueFromContractWithCustomerExcludingAssessedTax"
    return {
        "cik": APPLE,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                rev: {"units": {"USD": [
                    entry(ACC_23, "2023-09-30", 383285000000, "2022-09-25", frame="CY2023"),
                    entry(ACC_23, "2022-09-24", 394328000000, "2021-09-26"),  # comparative
                    entry(ACC_22, "2022-09-24", 394328000000, "2021-09-26"),
                    entry(ACC_Q, "2023-07-01", 81797000000, "2023-04-02", form="10-Q"),
                    entry(ACC_23, "2023-09-30", 90146000000, "2023-07-02"),  # a quarter: skipped
                ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    entry(ACC_23, "2023-09-30", 96995000000, "2022-09-25"),
                    entry(ACC_22, "2022-09-24", 99803000000, "2021-09-26"),
                ]}},
                "Assets": {"units": {"USD": [
                    entry(ACC_23, "2023-09-30", 352583000000),
                    entry(ACC_23, "2022-09-24", 352755000000),
                    entry(ACC_22, "2022-09-24", 352755000000),
                ]}},
            }
        },
    }  # fmt: skip


def row(accession: str, tag: str, end: date, qtrs: int, value: str, frame: str | None = None):
    return {
        "cik": APPLE,
        "accession": accession,
        "tag": tag,
        "period_end": end,
        "qtrs": qtrs,
        "value": Decimal(value),
        "calendar_frame": frame,
    }


REV = "RevenueFromContractWithCustomerExcludingAssessedTax"


def good_rows() -> list[dict[str, Any]]:
    return [
        row(ACC_23, REV, date(2023, 9, 30), 4, "383285000000.0000", "CY2023"),
        row(ACC_22, REV, date(2022, 9, 30), 4, "394328000000.0000"),  # FSDS month-end rounded
        row(ACC_23, "NetIncomeLoss", date(2023, 9, 30), 4, "96995000000.0000"),
        row(ACC_22, "NetIncomeLoss", date(2022, 9, 30), 4, "99803000000.0000"),
        row(ACC_23, "Assets", date(2023, 9, 30), 0, "352583000000.0000"),
        row(ACC_22, "Assets", date(2022, 9, 30), 0, "352755000000.0000"),
    ]


def run_apple(rows: list[dict[str, Any]]) -> dict[tuple[str, str], Comparison]:
    return {(c.accession, c.metric): c for c in compare((APPLE,), {APPLE: apple_cf()}, rows)}


def test_the_two_most_recent_annual_filings_are_chosen_newest_first():
    assert latest_annual_filings(apple_cf()) == [(ACC_23, "10-K"), (ACC_22, "10-K")]
    assert latest_annual_filings(apple_cf(), n=1) == [(ACC_23, "10-K")]


def test_quarterly_and_amended_filings_are_not_annual_filings():
    cf = apple_cf()
    cf["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(
        entry("0000320193-24-000001", "2023-12-30", 1, form="10-K/A", filed="2024-02-01")
    )

    assert [a for a, _ in latest_annual_filings(cf)] == [ACC_23, ACC_22]


def test_expected_fact_is_the_current_period_not_a_comparative_or_a_quarter():
    tag, e = expected_fact(apple_cf(), ACC_23, METRICS["revenue"]) or ("", {})

    assert tag == REV
    assert (e["start"], e["end"], e["val"]) == ("2022-09-25", "2023-09-30", 383285000000)


def test_expected_fact_falls_back_through_the_tag_list():
    cf = apple_cf()
    revenues = cf["facts"]["us-gaap"].pop(REV)
    cf["facts"]["us-gaap"]["Revenues"] = revenues  # only the second tag exists

    found = expected_fact(cf, ACC_22, METRICS["revenue"])

    assert found is not None and found[0] == "Revenues"


def test_expected_fact_is_none_when_the_filing_has_no_such_tag():
    assert expected_fact(apple_cf(), ACC_23, ("DoesNotExist",)) is None


def test_every_comparison_matches_when_curated_agrees_even_with_52_53_week_dates():
    """companyfacts has 2022-09-24, FSDS has 2022-09-30: the dates match within tolerance."""
    results = run_apple(good_rows())

    assert len(results) == 6
    assert {c.outcome for c in results.values()} == {"match"}
    assert results[(ACC_22, "revenue")].period_end == "2022-09-24"
    assert results[(ACC_22, "revenue")].curated_period_end == "2022-09-30"


def test_a_different_value_is_a_value_mismatch_with_both_values_recorded():
    rows = good_rows()
    rows[0] = row(ACC_23, REV, date(2023, 9, 30), 4, "383285000001.0000", "CY2023")

    c = run_apple(rows)[(ACC_23, "revenue")]

    assert c.outcome == "value_mismatch"
    assert c.companyfacts_value == "383285000000"
    assert c.curated_value == "383285000001.0000"
    assert c.classification is None  # classifying a mismatch is Shekar's job


def test_a_missing_curated_fact_is_reported_not_skipped():
    rows = [r for r in good_rows() if not (r["accession"] == ACC_23 and r["tag"] == "Assets")]

    c = run_apple(rows)[(ACC_23, "total_assets")]

    assert c.outcome == "missing_in_curated"
    assert c.curated_value is None


def test_the_same_tag_with_the_wrong_duration_or_a_distant_date_does_not_count():
    rows = good_rows()
    rows[0] = row(ACC_23, REV, date(2023, 9, 30), 3, "383285000000.0000")  # 9M, not FY
    rows[1] = row(ACC_22, REV, date(2022, 6, 30), 4, "394328000000.0000")  # 86 days away

    results = run_apple(rows)

    assert results[(ACC_23, "revenue")].outcome == "missing_in_curated"
    assert results[(ACC_22, "revenue")].outcome == "missing_in_curated"


def test_the_date_tolerance_boundary():
    inside = date(2022, 9, 24).toordinal() + DATE_TOLERANCE_DAYS
    outside = inside + 1
    rows = good_rows()
    rows[1] = row(ACC_22, REV, date.fromordinal(inside), 4, "394328000000.0000")
    assert run_apple(rows)[(ACC_22, "revenue")].outcome == "match"
    rows[1] = row(ACC_22, REV, date.fromordinal(outside), 4, "394328000000.0000")
    assert run_apple(rows)[(ACC_22, "revenue")].outcome == "missing_in_curated"


def test_a_metric_the_filing_does_not_have_is_missing_in_companyfacts():
    cf = apple_cf()
    del cf["facts"]["us-gaap"]["NetIncomeLoss"]

    results = compare((APPLE,), {APPLE: cf}, good_rows())

    c = next(c for c in results if c.metric == "net_income" and c.accession == ACC_23)
    assert c.outcome == "missing_in_companyfacts" and c.tag is None


def test_two_curated_rows_for_one_accession_tag_and_period_are_a_duplicate_not_a_match():
    """(fact_key, accession) is unique in facts, so two near rows under one accession is a bug,
    even when one of them has exactly the companyfacts value."""
    rows = [*good_rows(), row(ACC_23, REV, date(2023, 9, 30), 4, "999.0000")]

    c = run_apple(rows)[(ACC_23, "revenue")]

    assert c.outcome == "duplicate_in_curated"
    assert c.curated_value == "383285000000.0000,999.0000"


def test_only_us_gaap_curated_rows_are_compared_with_companyfacts_us_gaap_tags():
    custom = {**row(ACC_23, REV, date(2023, 9, 30), 4, "383285000000.0000"), "taxonomy": "custom"}
    gaap = {**good_rows()[0], "taxonomy": "us-gaap"}

    only_custom = [r for r in good_rows() if r["tag"] != REV or r["accession"] != ACC_23]
    assert run_apple([*only_custom, custom])[(ACC_23, "revenue")].outcome == "missing_in_curated"
    # a custom tag with the same name in the same filing is not a duplicate of the us-gaap one
    both = [r for r in good_rows() if not (r["tag"] == REV and r["accession"] == ACC_23)]
    assert run_apple([*both, gaap, custom])[(ACC_23, "revenue")].outcome == "match"


def test_a_tag_with_only_comparative_entries_in_the_filing_is_skipped_for_the_next_tag():
    """If the first tag only appears as a prior-year comparative, validating it would check the
    wrong period; the current-period fact under the next tag must be used instead."""
    cf = apple_cf()
    gaap = cf["facts"]["us-gaap"]
    gaap["SalesRevenueNet"] = {"units": {"USD": [entry(ACC_23, "2022-09-24", 7, "2021-09-26")]}}
    gaap["Revenues"] = gaap.pop(REV)  # current-period revenue lives under the 2nd tag
    tags = ("SalesRevenueNet", "Revenues")

    found = expected_fact(cf, ACC_23, tags)

    assert found is not None
    assert found[0] == "Revenues"
    assert found[1]["end"] == "2023-09-30"


def test_summary_reports_how_many_comparisons_were_expected():
    results = list(run_apple(good_rows()).values())

    s = summarize(results)

    assert s["expected_comparisons"] == 60  # 10 companies x 3 metrics x 2 filings
    assert s["n_comparisons"] == 6  # one company in this test: visibly incomplete


def test_calendar_frame_is_compared_when_both_sides_have_one():
    rows = good_rows()
    rows[0] = row(ACC_23, REV, date(2023, 9, 30), 4, "383285000000.0000", "CY2022")

    results = run_apple(rows)

    assert results[(ACC_23, "revenue")].frame_agrees is False  # companyfacts says CY2023
    assert results[(ACC_22, "revenue")].frame_agrees is None  # companyfacts has no frame there
    s = summarize(list(results.values()))
    assert s["frames_compared"] == 1 and s["frames_agree"] == 0


def test_summary_counts_outcomes_and_unclassified_non_matches():
    rows = good_rows()
    rows[0] = row(ACC_23, REV, date(2023, 9, 30), 4, "1.0000")
    results = list(run_apple(rows).values())

    before = summarize(results)
    assert before["n_comparisons"] == 6
    assert before["matched"] == 5
    assert before["outcomes"] == {"match": 5, "value_mismatch": 1}
    assert before["unclassified_non_matches"] == 1

    mismatch = next(c for c in results if c.outcome == "value_mismatch")
    mismatch.classification = "restatement"
    assert summarize(results)["unclassified_non_matches"] == 0


def test_ten_companies_three_metrics_two_filings_make_sixty_comparisons():
    assert len(SPOT_COMPANIES) == 10 and len(METRICS) == 3
    ciks = tuple(cik for cik, _ in SPOT_COMPANIES)
    cfs = {}
    for cik in ciks:
        cf = apple_cf()
        cf["cik"] = cik
        cfs[cik] = cf

    results = compare(ciks, cfs, [])

    assert len(results) == 60
    assert {c.outcome for c in results} == {"missing_in_curated"}


def test_companyfacts_url_is_zero_padded_to_ten_digits():
    assert (
        companyfacts_url(320193) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )


@pytest.fixture
def client() -> SecClient:
    bucket = TokenBucket(rate=8, clock=lambda: 0.0, sleep=lambda s: None)
    return SecClient(
        user_agent="Acme Research research-team@acme-research.org",
        bucket=bucket,
        sleep=lambda s: None,
        rng=random.Random(1),
    )


def test_fetch_all_gets_each_company_and_checks_the_entity_name(client: SecClient):
    with respx.mock(assert_all_called=True) as router:
        router.get(companyfacts_url(APPLE)).mock(
            return_value=httpx.Response(200, json={"entityName": "Apple Inc.", "facts": {}})
        )
        out = fetch_all(client, ((APPLE, "apple"),))

    assert set(out) == {APPLE}


def test_fetch_all_rejects_a_cik_that_is_not_the_expected_company(client: SecClient):
    with respx.mock(assert_all_called=True) as router:
        router.get(companyfacts_url(APPLE)).mock(
            return_value=httpx.Response(200, json={"entityName": "Somebody Else Corp"})
        )

        with pytest.raises(ValueError, match="expected a name containing"):
            fetch_all(client, ((APPLE, "apple"),))


# --------------------------------------------------------------------------------------
# Which filings are compared: the latest N (default, so a full-history run keeps working) or
# the 10-Ks of pinned fiscal years. A fiscal year is the company's OWN label, i.e. the `fy` of
# the filing (curated.facts.fiscal_year is defined the same way), not the year the period ends in.
# --------------------------------------------------------------------------------------

ACC_24 = "0000320193-24-000123"  # a FY2024 10-K, newer than the other two


def apple_cf_with_fy2024() -> dict[str, Any]:
    cf = apple_cf()
    cf["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(
        entry(ACC_24, "2024-09-28", 364980000000, fy=2024, filed="2024-11-01")
    )
    return cf


def test_default_selection_is_the_latest_n_annual_filings():
    assert select_filings(apple_cf_with_fy2024()) == [(ACC_24, "10-K"), (ACC_23, "10-K")]
    assert select_filings(apple_cf_with_fy2024(), n=1) == [(ACC_24, "10-K")]


def test_pinned_fiscal_years_ignore_recency_and_come_back_newest_first():
    cf = apple_cf_with_fy2024()

    assert select_filings(cf, fiscal_years=(2022, 2023)) == [(ACC_23, "10-K"), (ACC_22, "10-K")]
    assert select_filings(cf, fiscal_years=(2022,)) == [(ACC_22, "10-K")]


def test_a_pinned_fiscal_year_without_a_10_k_is_an_error_naming_the_year():
    with pytest.raises(ValueError, match="2019"):
        select_filings(apple_cf(), fiscal_years=(2019, 2023))


def test_the_fiscal_year_is_the_companys_label_not_the_year_the_period_ends_in():
    """A retailer whose year ends 2023-01-28 and which calls it fiscal 2022 (Target, Home Depot)."""
    acc = "0000027419-23-000015"
    cf = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            entry(acc, "2023-01-28", 1, fy=2022, filed="2023-03-08"),
                            entry(acc, "2022-01-29", 1, fy=2022, filed="2023-03-08"),
                        ]
                    }
                }
            }
        }
    }

    assert select_filings(cf, fiscal_years=(2022,)) == [(acc, "10-K")]
    with pytest.raises(ValueError, match="2023"):
        select_filings(cf, fiscal_years=(2023,))


def test_the_filings_fiscal_year_is_the_most_common_fy_among_its_entries():
    cf = apple_cf()
    cf["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(entry(ACC_23, "2023-09-30", 1, fy=2099))

    assert select_filings(cf, fiscal_years=(2023,)) == [(ACC_23, "10-K")]


def test_two_annual_filings_in_one_fiscal_year_resolve_to_the_latest_filed():
    """A 10-KT (transition report) can carry the same fy as the 10-K before it."""
    transition = "0000320193-23-000200"
    cf = apple_cf()
    cf["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(
        entry(transition, "2023-12-30", 5, fy=2023, form="10-KT", filed="2024-02-01")
    )

    assert select_filings(cf, fiscal_years=(2023,)) == [(transition, "10-KT")]


def test_compare_with_pinned_years_compares_only_those_filings():
    cf = apple_cf_with_fy2024()

    results = compare((APPLE,), {APPLE: cf}, good_rows(), fiscal_years=(2022,))

    assert {c.accession for c in results} == {ACC_22}
    assert len(results) == 3 and {c.outcome for c in results} == {"match"}


def test_compare_defaults_to_the_latest_filings_when_no_years_are_pinned():
    cf = apple_cf_with_fy2024()

    results = compare((APPLE,), {APPLE: cf}, good_rows())

    assert {c.accession for c in results} == {ACC_24, ACC_23}


def test_summary_can_be_told_how_many_comparisons_were_expected():
    results = list(run_apple(good_rows()).values())

    assert summarize(results, expected=6)["expected_comparisons"] == 6
    assert summarize(results)["expected_comparisons"] == 60  # the default is unchanged


def test_fiscal_years_argument_parses_a_comma_separated_list():
    assert parse_fiscal_years("2022,2023") == (2022, 2023)
    assert parse_fiscal_years(" 2023 ") == (2023,)


@pytest.mark.parametrize("bad", ["", "2022,", "abc", "2022,x", "1899", "2022,2022"])
def test_fiscal_years_argument_rejects_bad_input(bad: str):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        parse_fiscal_years(bad)


def test_an_amended_10_k_is_never_selected_for_a_pinned_year():
    cf = apple_cf()
    cf["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(
        entry("0000320193-24-000001", "2023-09-30", 1, form="10-K/A", fy=2023, filed="2024-02-01")
    )

    assert select_filings(cf, fiscal_years=(2023,)) == [(ACC_23, "10-K")]


def test_a_non_integer_fy_is_ignored_and_never_breaks_the_default_selection():
    cf = apple_cf()
    units = cf["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    units.append(entry(ACC_23, "2023-09-30", 1, fy="FY2023"))
    units.append(entry(ACC_22, "2022-09-24", 1, fy=""))
    units.append(entry(ACC_22, "2022-09-24", 1, fy=None))

    assert select_filings(cf) == [(ACC_23, "10-K"), (ACC_22, "10-K")]
    assert select_filings(cf, fiscal_years=(2022, 2023)) == [(ACC_23, "10-K"), (ACC_22, "10-K")]


def test_a_filing_with_no_fy_is_not_found_by_year_and_the_error_says_how_many_lack_it():
    cf = {
        "entityName": "Odd Co",
        "facts": {
            "us-gaap": {"Assets": {"units": {"USD": [entry(ACC_23, "2023-09-30", 1, fy=None)]}}}
        },
    }

    with pytest.raises(ValueError, match=r"2023.*1 annual filings? found, 1 without fy"):
        select_filings(cf, fiscal_years=(2023,))
    assert select_filings(cf) == [(ACC_23, "10-K")]  # the default does not need fy


@pytest.mark.parametrize("first_year", [2022, 2023])
def test_a_tie_between_two_fy_values_resolves_to_the_later_year_whatever_the_entry_order(
    first_year: int,
):
    other = 2022 if first_year == 2023 else 2023
    acc = "0000320193-23-000999"
    cf = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            entry(acc, "2023-09-30", 1, fy=first_year, filed="2023-11-03"),
                            entry(acc, "2022-09-24", 1, fy=other, filed="2023-11-03"),
                        ]
                    }
                }
            }
        }
    }

    assert select_filings(cf, fiscal_years=(2023,)) == [(acc, "10-K")]
    with pytest.raises(ValueError):
        select_filings(cf, fiscal_years=(2022,))
