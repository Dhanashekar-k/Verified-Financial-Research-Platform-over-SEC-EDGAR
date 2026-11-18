"""Tests for ``pipelines/parsing/tables.py``: cell cleanup, numeric detection, header rows."""

from __future__ import annotations

import pytest

from pipelines.parsing.tables import (
    clean_cells,
    header_row_count,
    is_data_table,
    is_numeric_cell,
    render_row,
)


@pytest.mark.parametrize(
    "cell", ["1,234", "$1,234.5", "(1,234)", "12%", "$ (45)", "0.3", "—", "-", "(0.5)%", "2023"]
)
def test_numeric_cells(cell: str):
    assert is_numeric_cell(cell)


@pytest.mark.parametrize("cell", ["Net sales", "Q3 2023", "10-K", "", "Item 1A.", "Total assets"])
def test_non_numeric_cells(cell: str):
    assert not is_numeric_cell(cell)


def test_clean_cells_drops_empties_and_glues_currency_and_parens():
    assert clean_cells(["Net sales", "$", "383,285", "", "$", "(1,200", ")", "5", "%"]) == (
        "Net sales",
        "$383,285",
        "$(1,200)",
        "5%",
    )


def test_clean_cells_keeps_a_lone_symbol_cell_text_if_nothing_to_glue():
    assert clean_cells(["Note", "$"]) == ("Note", "$")


def test_render_row_joins_cells_with_pipes():
    assert render_row(("Net sales", "$383,285", "$394,328")) == "Net sales | $383,285 | $394,328"


def test_header_rows_are_the_leading_rows_with_no_amounts_other_than_years():
    rows = [
        ("In millions", "2023", "2022"),
        ("Net sales", "$383,285", "$394,328"),
        ("Gross margin", "169,148", "170,782"),
    ]

    assert header_row_count(rows) == 1


def test_a_table_whose_first_row_is_data_has_no_header():
    assert header_row_count([("Net sales", "383,285", "394,328"), ("Cost", "1", "2")]) == 0


def test_header_is_capped_at_three_rows():
    rows = [("a",), ("b",), ("c",), ("d",), ("x", "1,000", "2,000")]

    assert header_row_count(rows) == 3


def test_data_table_needs_two_rows_and_three_amounts():
    assert is_data_table([("a", "1,000", "2,000"), ("b", "3", "4")])
    assert not is_data_table([("Item 1A.", "Risk Factors")])
    assert not is_data_table([("only", "1", "2")])  # one row
    assert not is_data_table([("a", "1"), ("b", "2")])  # two amounts
    assert not is_data_table([("2023", "2022"), ("Item", "Name")])  # years alone are not amounts
