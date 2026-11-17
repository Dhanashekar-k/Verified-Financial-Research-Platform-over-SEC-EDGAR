"""Property tests (Hypothesis) for version selection, `select_versions_ref`.

The example-based scenarios live in `tests/unit/test_versions.py`.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.common.fiscal_ref import FactVersion, SelectedVersion, select_versions_ref

_VALUES = [Decimal("100.0000"), Decimal("90.0000"), Decimal("110.5000")]


_PERIOD_END = date(2022, 12, 31)


_OTHER_PERIOD = date(2023, 3, 31)


@st.composite
def version_inputs(draw: st.DrawFn) -> list[FactVersion]:
    """1-3 fact_keys with 1-5 versions each: filed dates collide often (ties), accessions are
    unique and unrelated to filing order, filing_period equals period_end or not, and values
    repeat so that restated and never-restated groups both occur. Input order is shuffled.
    """
    versions: list[FactVersion] = []
    for key in range(1, draw(st.integers(1, 3)) + 1):
        n = draw(st.integers(1, 5))
        order = draw(st.permutations(range(n)))
        for i in range(n):
            form = draw(st.sampled_from(["10-K", "10-K/A", "10-Q"]))
            versions.append(
                FactVersion(
                    fact_key=key,
                    fact_id=key * 100 + i,
                    period_end=_PERIOD_END,
                    value=draw(st.sampled_from(_VALUES)),
                    accession=f"{key:010d}-23-{order[i]:06d}",
                    form=form,
                    filed=date(2023, 2, 1) + timedelta(days=draw(st.integers(0, 3))),
                    is_amendment=form.endswith("/A"),
                    filing_period=draw(st.sampled_from([_PERIOD_END, _OTHER_PERIOD])),
                )
            )
    return draw(st.permutations(versions))


@settings(max_examples=300)
@given(versions=version_inputs())
def test_version_selection_invariants_property(versions: list[FactVersion]) -> None:
    """(9) Per fact_key: exactly one is_latest row and exactly one is_original row; 1 or 2 rows;
    n_versions == the group's input size; is_restated == (more than one distinct value); the
    latest row is the max (filed, accession) (when two rows); the original is the min (filed,
    accession) among filing_period == period_end versions (or among all if none); one row <=>
    same value, and that one row is the ORIGINAL version.
    """
    out = select_versions_ref(versions)
    assert all(r.version in versions for r in out)
    for key in {v.fact_key for v in versions}:
        group = [v for v in versions if v.fact_key == key]
        rows = [r for r in out if r.version.fact_key == key]
        assert 1 <= len(rows) <= 2
        assert sum(r.is_latest for r in rows) == 1
        assert sum(r.is_original for r in rows) == 1
        assert all(r.n_versions == len(group) for r in rows)
        assert all(r.is_restated == (len({v.value for v in group}) > 1) for r in rows)

        latest = max(group, key=lambda v: (v.filed, v.accession))
        eligible = [v for v in group if v.filing_period == v.period_end] or group
        original = min(eligible, key=lambda v: (v.filed, v.accession))
        latest_row = next(r for r in rows if r.is_latest)
        original_row = next(r for r in rows if r.is_original)
        assert original_row.version.value == original.value
        if latest.value == original.value:
            assert len(rows) == 1
            assert latest_row is original_row
            # The single collapsed row cites the native filing: the ORIGINAL version.
            assert (original_row.version.filed, original_row.version.accession) == (
                original.filed,
                original.accession,
            )
        else:
            assert (latest_row.version.filed, latest_row.version.accession) == (
                latest.filed,
                latest.accession,
            )
            assert len(rows) == 2
            assert latest_row is not original_row
            assert not latest_row.is_original and not original_row.is_latest
            assert (original_row.version.filed, original_row.version.accession) == (
                original.filed,
                original.accession,
            )


@settings(max_examples=100)
@given(versions=version_inputs(), data=st.data())
def test_version_selection_ignores_input_order_property(
    versions: list[FactVersion], data: st.DataObject
) -> None:
    """(8, property) Any permutation of the input gives the same set of output rows."""

    def signature(rows: list[SelectedVersion]) -> Counter[tuple[object, ...]]:
        return Counter(
            (r.version.fact_id, r.is_latest, r.is_original, r.n_versions, r.is_restated)
            for r in rows
        )

    shuffled = data.draw(st.permutations(versions))
    assert signature(select_versions_ref(shuffled)) == signature(select_versions_ref(versions))
