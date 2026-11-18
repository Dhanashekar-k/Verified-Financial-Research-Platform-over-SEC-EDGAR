"""Table helpers (M02 section 3.3 step 2): clean cells, spot data tables, find header rows.

A *data table* has numbers worth keeping row-wise (`Row label | col1 | col2`). Anything else
(layout tables, heading tables like `Item 1A. | Risk Factors`) is read as ordinary paragraphs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_NUMBER = re.compile(r"^\(?\s*\$?\s*\(?\s*[\d][\d,]*(?:\.\d+)?\s*\)?\s*%?\s*\)?%?$")
_DASH = {"—", "–", "-", "—%"}
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
MAX_HEADER_ROWS = 3
MIN_AMOUNTS = 3


def is_numeric_cell(cell: str) -> bool:
    text = cell.strip()
    return bool(text) and (text in _DASH or bool(_NUMBER.match(text)))


def _is_amount(cell: str) -> bool:
    """A number that is not just a year or a dash (years head columns; dashes mean 'nil')."""
    text = cell.strip()
    return is_numeric_cell(text) and text not in _DASH and not _YEAR.match(text)


def clean_cells(cells: Sequence[str]) -> tuple[str, ...]:
    """Drop empty cells and glue `$`, `(`, `)` and `%` fragments onto their numbers."""
    out: list[str] = []
    pending = ""
    for raw in cells:
        cell = raw.strip()
        if not cell:
            continue
        if cell in ("$", "(", "$(", "($"):
            pending += cell
            continue
        if cell in (")", "%", ")%", "%)") and out:
            out[-1] += cell
            continue
        out.append(pending + cell)
        pending = ""
    if pending:
        out.append(pending)
    return tuple(out)


def render_row(cells: Sequence[str]) -> str:
    return " | ".join(cells)


def is_data_table(rows: Sequence[Sequence[str]]) -> bool:
    if len(rows) < 2:
        return False
    return sum(_is_amount(c) for row in rows for c in row) >= MIN_AMOUNTS


def header_row_count(rows: Sequence[Sequence[str]]) -> int:
    """Leading rows before the first row that carries an amount (years do not count), max 3."""
    for i, row in enumerate(rows[: MAX_HEADER_ROWS + 1]):
        if any(_is_amount(c) for c in row):
            return min(i, MAX_HEADER_ROWS)
    return min(len(rows), MAX_HEADER_ROWS)
