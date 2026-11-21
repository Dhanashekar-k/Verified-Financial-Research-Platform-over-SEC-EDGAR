"""`eval.reparse_compare`: the numbers the ADR-031 gate and report are read from."""

from __future__ import annotations

from typing import Any

from eval.reparse_compare import compare


def row(
    acc: str,
    status: str,
    *,
    cik: int = 1,
    form: str = "10-Q",
    err: str | None = None,
    sections: int = 5,
    chunks: int = 50,
    source: str = "edgar_html",
) -> dict[str, Any]:
    return {
        "accession": acc,
        "cik": cik,
        "form": form,
        "doc_source": source,
        "parse_status": status,
        "parse_error": err,
        "n_sections": sections,
        "n_chunks": chunks,
    }


BOTH = "missing sections: P1-1, P1-2"


def test_recovery_and_strict_success_are_counted_and_the_gate_is_clean():
    before = {
        r["accession"]: r
        for r in [
            row("a", "partial", cik=7, err=BOTH),
            row("b", "partial", cik=7, err=BOTH),
            row("c", "ok", cik=8),
            row("d", "partial", cik=8, err="missing sections: P1-2"),
        ]
    }
    after = {
        r["accession"]: r
        for r in [
            row("a", "ok", cik=7),  # recovered
            row("b", "partial", cik=7, err=BOTH),  # still missing both
            row("c", "ok", cik=8),  # unchanged
            row("d", "partial", cik=8, err="missing sections: P1-2"),
        ]
    }

    out = compare(before, after)

    assert out["html"]["strict_before"] == 0.25 and out["html"]["strict_after"] == 0.5
    assert out["transitions"] == {"ok->ok": 1, "partial->ok": 1, "partial->partial": 2}
    assert out["gate"]["previously_ok_not_ok"] == 0 and out["gate"]["previously_ok_changed"] == 0
    assert out["ten_q_missing_both_p1"] == {"before": 2, "after": 1, "recovered_to_ok": 1}
    assert out["companies_with_both_missing_before"]["n"] == 1
    assert out["companies_with_both_missing_before"]["with_at_least_one_filing_now_ok"] == 1
    assert out["companies_with_both_missing_before"]["with_every_affected_filing_now_ok"] == 0
    assert out["companies_with_both_missing_before"]["still_with_a_filing_missing_both"] == 1


def test_a_previously_ok_filing_that_is_no_longer_ok_is_a_downgrade_and_breaks_the_gate():
    before = {"a": row("a", "ok")}
    after = {"a": row("a", "partial", err="missing sections: P1-2")}

    gate = compare(before, after)["gate"]

    assert gate["status_downgrades"] == 1
    assert gate["previously_ok_not_ok"] == 1
    assert gate["previously_ok_changed"] == 1


def test_a_previously_ok_filing_whose_chunks_changed_is_reported_even_if_still_ok():
    before = {"a": row("a", "ok", chunks=50)}
    after = {"a": row("a", "ok", chunks=51)}

    gate = compare(before, after)["gate"]

    assert gate["previously_ok_not_ok"] == 0
    assert gate["previously_ok_changed"] == 1


def test_ten_k_and_financebench_rows_are_checked_separately():
    before = {
        "k": row("k", "ok", form="10-K"),
        "f": row("f", "ok", form="10-K", source="financebench_pdf"),
    }
    after = {
        "k": row("k", "ok", form="10-K", chunks=60),
        "f": row("f", "ok", form="10-K", source="financebench_pdf", chunks=60),
    }

    gate = compare(before, after)["gate"]

    assert gate["form_10k_rows_changed"] == 2  # both rows carry a 10-K form
    assert gate["financebench_rows_changed"] == 1
