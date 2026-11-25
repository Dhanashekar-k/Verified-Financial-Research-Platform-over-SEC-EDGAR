"""resolve_company (M05 section 3.2): ticker -> name -> alias -> rapidfuzz >= 90, else ambiguous.

The index is the committed snapshot tests/fixtures/entities/companies.csv (the tracked S&P 500
plus the FinanceBench companies; made by tests/fixtures/make_entities_fixture.py) and the shipped
aliases.csv. No database, no network.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.entities.resolve_company import (
    ALIASES_PATH,
    CompanyIndex,
    CompanyRecord,
    clarification_for,
    load_aliases,
    normalize_name,
    normalize_ticker,
    resolve_company,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "entities" / "companies.csv"
APPLE, AMAT, GOOGLE, META, JPM = 320193, 6951, 1652044, 1326801, 19617
BERKSHIRE, COKE, PEPSI, JNJ, JCI, ATT = 1067983, 21344, 77476, 200406, 833444, 732717


@pytest.fixture(scope="module")
def index() -> CompanyIndex:
    return CompanyIndex.from_csv(FIXTURE)


def ciks(mention: str, index: CompanyIndex) -> list[int]:
    return [m.cik for m in resolve_company(mention, index)]


# --- the cases M05 names -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mention", ["AAPL", "Apple", "Apple Inc.", "APPLE INC", "Apple's", "$AAPL"]
)
def test_apple_resolves_to_apple_only(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [APPLE]


@pytest.mark.parametrize("mention", ["Applied Materials", "AMAT", "Applied Materials, Inc."])
def test_applied_materials_is_not_apple(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [AMAT]


@pytest.mark.parametrize(
    "mention",
    ["Alphabet", "Alphabet Inc.", "Google", "GOOG", "GOOGL", "Alphabet Class C", "YouTube"],
)
def test_alphabet_and_google_are_one_cik(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [GOOGLE]


@pytest.mark.parametrize("mention", ["Meta", "META", "Meta Platforms", "Facebook", "Instagram"])
def test_meta_and_facebook(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [META]


@pytest.mark.parametrize(
    "mention", ["JPM", "JPMorgan", "JPMorgan Chase", "JP Morgan", "JPMorgan Chase & Co.", "Chase"]
)
def test_jpmorgan(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [JPM]


@pytest.mark.parametrize(
    "mention",
    [
        "Berkshire Hathaway",
        "Berkshire",
        "BRK.B",
        "BRK-B",
        "BRK.A",
        "BRK.b",
        "Berkshire Hathaway Class B",
        "Berkshire Hathaway Inc. Class A",
    ],
)
def test_berkshire_classes_share_one_cik(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [BERKSHIRE]


@pytest.mark.parametrize(
    "mention", ["Coke", "Coca-Cola", "Coca Cola", "The Coca-Cola Company", "KO"]
)
def test_coke_is_coca_cola(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [COKE]


def test_other_aliases(index: CompanyIndex) -> None:
    assert ciks("Pepsi", index) == [PEPSI]
    assert ciks("J&J", index) == [JNJ]
    assert ciks("Johnson & Johnson", index) == [JNJ]
    assert ciks("AT&T", index) == [ATT]


# --- stages and precedence ---------------------------------------------------------------------


def test_each_stage_is_labelled(index: CompanyIndex) -> None:
    assert resolve_company("AAPL", index)[0].how == "ticker"
    assert resolve_company("Apple Inc.", index)[0].how == "name"
    assert resolve_company("Coke", index)[0].how == "alias"
    fuzzy = resolve_company("Microsft", index)
    assert [m.how for m in fuzzy] == ["fuzzy"] and 90 <= fuzzy[0].score < 100


def test_exact_name_beats_fuzzy(index: CompanyIndex) -> None:
    """ "Apple" is a fuzzy subset of longer names; the exact stage must answer first."""
    matches = resolve_company("Apple", index)
    assert [(m.cik, m.how) for m in matches] == [(APPLE, "name")]


def test_a_ticker_wins_over_a_fuzzy_name(index: CompanyIndex) -> None:
    assert [(m.cik, m.how) for m in resolve_company("AMAT", index)] == [(AMAT, "ticker")]


def test_only_a_mention_written_as_a_ticker_is_a_ticker(index: CompanyIndex) -> None:
    assert ciks("A", index) != [] and resolve_company("A", index)[0].how == "ticker"  # Agilent
    assert ciks("T", index) == [ATT]
    for word in ("a", "t", "it", "all", "key", "low", "now", "are", "has", "ice", "well", "fast"):
        assert ciks(word, index) == [], word  # words, not Allstate/KeyCorp/Lowe's/ServiceNow/...
    assert ciks("aapl", index) == []  # a lower-case ticker asks instead of guessing
    assert ciks("BRK.b", index) == [BERKSHIRE]  # a share-class ticker keeps its dot or dash


def test_dollar_prefix_and_whitespace(index: CompanyIndex) -> None:
    assert ciks("  $AAPL ", index) == [APPLE]


# --- ambiguity and misses ----------------------------------------------------------------------


def test_johnson_is_not_guessed_but_both_are_offered(index: CompanyIndex) -> None:
    assert ciks("Johnson", index) == []  # a bare word is a subset of two names: never resolved
    assert {m.cik for m in index.suggest("Johnson")} >= {JNJ, JCI}
    text = clarification_for("Johnson", index.suggest("Johnson"))
    assert "johnson & johnson" in text.lower() and "Which one" in text


def test_a_multi_word_prefix_of_one_longer_name_resolves(index: CompanyIndex) -> None:
    assert ciks("Johnson Controls", index) == [JCI]


@pytest.mark.parametrize(
    "mention",
    [
        "Apple Hospitality REIT",  # a different company that contains "Apple"
        "Johnson Matthey",
        "Dow Jones",
        "Berkshire Hathaway Energy",  # a different filer
        "Tesla and Ford",  # two companies in one mention: never silently the first
        "Microsoft Xbox",
        "First",
        "Western",
        "Applied",
        "Advanced",
        "Home",
        "Depot",
        "Street",
        "Honeywell Aerospace Holdings",
    ],
)
def test_a_subset_of_a_name_never_resolves_to_one_wrong_company(
    mention: str, index: CompanyIndex
) -> None:
    assert ciks(mention, index) == []


def test_suggestions_are_never_resolutions(index: CompanyIndex) -> None:
    assert [m.cik for m in index.suggest("Applied")] == [AMAT]
    text = clarification_for("Applied", index.suggest("Applied"))
    assert text.startswith("I did not recognize 'Applied'")
    assert "applied materials" in text.lower()


@pytest.mark.parametrize(
    ("mention", "cik"),
    [("Lowe's", 60667), ("McDonald's", 63908), ("Moody's", 1059556), ("Honeywell", 773840)],
)
def test_possessives_and_brand_shortenings(mention: str, cik: int, index: CompanyIndex) -> None:
    assert ciks(mention, index) == [cik]


def test_every_financebench_company_name_resolves(index: CompanyIndex) -> None:
    """The names FinanceBench's authors use for their 32 companies are the realistic mentions."""
    path = Path(__file__).resolve().parents[2] / "fixtures" / "entities" / "financebench.csv"
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            assert ciks(row["company"], index) == [int(row["cik"])], row["company"]


def test_a_single_generic_word_is_never_a_fuzzy_resolution(index: CompanyIndex) -> None:
    """The first word of any company name must never resolve through the fuzzy stage: only a
    ticker, an exact name or an alias may answer a bare word."""
    for row in _rows():
        words = normalize_name(row["name"]).split()
        for m in resolve_company(words[0], index):
            assert m.how != "fuzzy", (words[0], row["name"], m)


@pytest.mark.parametrize(
    "mention", ["", "   ", "the", "Inc.", "Zzyzx Quantum Holdings", "xq", "?!"]
)
def test_unknown_mentions_return_nothing(mention: str, index: CompanyIndex) -> None:
    assert resolve_company(mention, index) == []
    if mention.strip():
        assert "could not find" in clarification_for(mention, [])


def test_no_cik_is_ever_guessed_from_a_near_miss(index: CompanyIndex) -> None:
    assert ciks("Aple", index) == []  # below the cutoff


@pytest.mark.parametrize(
    "mention",
    [
        "Cooper Technologies",  # one letter from Roper, but not a typo of it
        "Rogers Technologies",
        "Dyer Technologies",  # Tyler
        "Micro Technology",  # Micron or Microchip: never guess
        "American Express Bank",  # a subsidiary, not American Express Co
        "Mircosoft Corp Inc Holdings",
        "Nikes",  # a short word one edit from a real name asks instead of guessing
    ],
)
def test_a_lookalike_name_is_not_a_typo(mention: str, index: CompanyIndex) -> None:
    assert ciks(mention, index) == []


def test_no_name_is_a_typo_of_two_companies_or_resolves_to_another(index: CompanyIndex) -> None:
    """Corrupt each company's own name by one letter (a real typo): it must resolve to that
    company or to nothing, never to a different one."""
    wrong: list[tuple[str, str]] = []
    for row in _rows():
        words = normalize_name(row["name"]).split()
        i = max(range(len(words)), key=lambda k: len(words[k]))
        w = words[i]
        if len(w) < 6:
            continue
        words[i] = w[:2] + w[3] + w[2] + w[4:]  # swap two inner letters
        got = ciks(" ".join(words), index)
        if got and got != [int(row["cik"])]:
            wrong.append((row["name"], " ".join(words)))
    assert wrong == []


def test_typos_still_resolve(index: CompanyIndex) -> None:
    assert ciks("Microsft", index) == [789019]
    assert ciks("Applied Material", index) == [AMAT]


# --- the Postgres loader (a fake connection: the unit under test is the index builder) ---------


class _FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object) -> _FakeResult:
        self.calls.append((sql, params))
        return _FakeResult(self.rows)


def test_from_connection_builds_the_index_and_skips_aliases_of_absent_companies() -> None:
    conn = _FakeConn(
        [
            (320193, "Apple Inc.", "AAPL", None),
            (1652044, "Alphabet Inc.", "GOOGL", ["GOOG"]),
        ]
    )
    idx = CompanyIndex.from_connection(conn, [320193, 1652044, 1326801])  # type: ignore[arg-type]
    sql, params = conn.calls[0]
    assert "cik = ANY(%s)" in sql and params == ([320193, 1652044, 1326801],)
    assert [m.cik for m in idx.resolve("GOOG")] == [GOOGLE]
    assert [m.cik for m in idx.resolve("Google")] == [GOOGLE]  # alias of a present company
    assert idx.resolve("Facebook") == []  # alias of Meta, which this table lacks: skipped
    assert idx.resolve("Apple")[0].cik == APPLE


# --- every company resolves to itself ----------------------------------------------------------


def _rows() -> list[dict[str, str]]:
    with FIXTURE.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_every_ticker_resolves_to_its_own_company(index: CompanyIndex) -> None:
    for row in _rows():
        for ticker in [row["ticker"], *row["aliases"].split("|")]:
            if ticker:
                assert ciks(ticker, index) == [int(row["cik"])], (ticker, row["name"])


def test_every_name_resolves_to_a_set_containing_itself(index: CompanyIndex) -> None:
    """A company's own SEC name must find it. Collisions (two CIKs, one normalized name) show as
    several matches, never as a miss."""
    collisions: list[str] = []
    for row in _rows():
        found = ciks(row["name"], index)
        assert int(row["cik"]) in found, row["name"]
        if len(found) > 1:
            collisions.append(row["name"])
    assert collisions == []


# --- the alias file ----------------------------------------------------------------------------


def test_alias_file_points_at_tracked_companies_and_collides_with_nothing(
    index: CompanyIndex,
) -> None:
    aliases = load_aliases(ALIASES_PATH)
    assert len({normalize_name(a) for a, _ in aliases}) == len(aliases)
    names = {normalize_name(r["name"]) for r in _rows()}
    for alias, cik in aliases:
        assert cik in index.records, alias
        assert normalize_name(alias) not in names, f"{alias} is already an exact company name"


def test_an_alias_to_an_unknown_cik_is_refused() -> None:
    with pytest.raises(ValueError, match="not in the index"):
        CompanyIndex([CompanyRecord(cik=1, name="One Inc")], [("Uno", 2)])


# --- normalization -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Apple Inc.", "apple"),
        ("APPLIED MATERIALS INC /DE", "applied materials"),
        ("Procter & Gamble Co", "procter gamble"),
        ("The Coca-Cola Company", "coca cola"),
        ("Apple's", "apple"),
        ("Alphabet Inc. (Class C)", "alphabet"),
        ("Co", ""),  # nothing but corporate noise: no company is meant
        ("CORNING INC /NY", "corning"),
        ("New York Times Co", "new york times"),
        ("Altria Group Inc /NEW", "altria group"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_ticker() -> None:
    assert normalize_ticker("BRK.B") == normalize_ticker("brk-b") == "BRKB"


# --- properties --------------------------------------------------------------------------------


@given(st.text(max_size=60))
def test_resolve_never_raises_and_is_unique_by_cik(mention: str) -> None:
    idx = _module_index()
    matches = resolve_company(mention, idx)
    assert len({m.cik for m in matches}) == len(matches)
    assert all(m.cik in idx.records and 0 < m.score <= 100 for m in matches)
    assert matches == resolve_company(mention, idx)  # deterministic


_INDEX: CompanyIndex | None = None


def _module_index() -> CompanyIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = CompanyIndex.from_csv(FIXTURE)
    return _INDEX


# --- SEC legal names that the usual name does not resemble (planner dev run, 2026-11-25) --------


@pytest.mark.parametrize(
    ("mention", "cik"),
    [
        ("CRH PLC", 849395),
        ("CRH plc's", 849395),
        ("D.R. Horton", 882184),
        ("D.R. Horton, Inc.", 882184),
        ("D.R. Horton's", 882184),
    ],
)
def test_names_registered_unlike_their_common_form(
    mention: str, cik: int, index: CompanyIndex
) -> None:
    assert ciks(mention, index) == [cik]
