"""Company mention -> CIK (M05 section 3.2): code resolves companies, the LLM never returns a CIK.

Stages, first one that finds anything wins: exact ticker (an ALL-CAPS mention) -> exact
normalized name -> alias -> guarded rapidfuzz over the tracked companies. More than one distinct
CIK from a stage means the mention is ambiguous (``needs_clarification``); no match returns ``[]``.
A wrong CIK is worse than a miss, so the fuzzy stage is strict. It accepts only (a) a typo: the same
number of words as a company name, every word equal except one that is a single edit away (a
transposition counts as one) and at least 6 letters long, so "Microsft" is Microsoft but "Cooper
Technologies" is not Roper Technologies and "Micro Technology" is not Micron; (b) a multi-word
mention that is the start of one company's longer name ("Johnson Controls" for "Johnson Controls
International"). ``token_set_ratio`` alone scores 100 for any subset ("Apple Hospitality REIT"
contains "Apple"; "First" is inside "First Solar") and ``ratio`` is dominated by a shared suffix, so
neither decides. Brand shortenings that are not typos ("Berkshire", "Disney") are in aliases.csv;
``suggest`` offers the unguarded token-set candidates for the clarification question only.

Names are normalized (case, punctuation, ``&``, possessives, corporate suffixes, share classes) so
"Coca-Cola", "COCA COLA CO" and "Coca Cola" meet. The index is built from records, so tests need
no database; at runtime it is built from the serving ``companies`` table (`from_connection`).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz, process
from rapidfuzz.distance import OSA

FUZZY_CUTOFF = 90
MIN_FUZZY_LEN = 3  # a 1-2 letter fragment is a subset of too many names to mean anything
MIN_TYPO_WORD = 6  # a shorter word is one edit from too many other names (roper/cooper)
ALIASES_PATH = Path(__file__).resolve().parent / "aliases.csv"
_BRACKETED = re.compile(r"\s*\([^()]*\)")

# Words that never tell two companies apart: dropped anywhere in a name.
_NOISE = frozenset(
    "inc incorporated corp corporation co company companies ltd limited plc llc lp".split()
)
# Noise only at the end ("... N.V.", "... /NEW"); kept inside "New York Times".
_TAIL_NOISE = frozenset("nv sa ag de new".split())
_STOP = frozenset({"and", "the"})
_TICKER_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}")

MatchKind = Literal["ticker", "name", "alias", "fuzzy"]


class CompanyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    cik: int
    name: str
    ticker: str | None = None
    tickers: list[str] = []  # extra tickers of the same CIK (share classes: GOOG, BRK-A)


class CompanyMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    cik: int
    name: str
    ticker: str | None
    how: MatchKind
    score: float  # 100 for the exact stages, the rapidfuzz score for "fuzzy"


def name_variants(text: str) -> list[str]:
    """Normalized forms of a mention, best first. A possessive is ambiguous: "Apple's" is "apple"
    but "Lowe's" is "lowes" (EDGAR writes LOWES COMPANIES INC), so both are tried."""
    stripped = normalize_name(text)
    glued = normalize_name(re.sub(r"['\u2019]", "", text))
    return [v for v in dict.fromkeys([stripped, glued]) if v]


def normalize_name(text: str) -> str:
    """Lower-case words of ``text`` without punctuation, possessives, share classes, the
    stop-words "the"/"and" or corporate suffixes ("Inc", "Corp /DE"). Empty when nothing but
    such words is left, so "Inc." matches no company."""
    s = text.casefold().replace("&", " and ")
    s = re.sub(r"/\s*[a-z]{2,3}\b", " ", s)  # EDGAR state suffixes: "CORNING INC /NY"
    s = re.sub(r"['\u2019]s\b", "", s)
    s = re.sub(r"\bclass\s+[a-z]\b", " ", s)
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _STOP and w not in _NOISE]
    while words and words[-1] in _TAIL_NOISE:
        words.pop()
    return " ".join(words)


def normalize_ticker(text: str) -> str:
    """``BRK.B`` and ``BRK-B`` are the same ticker."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


class CompanyIndex:
    def __init__(
        self, records: Iterable[CompanyRecord], aliases: Iterable[tuple[str, int]] = ()
    ) -> None:
        self.records: dict[int, CompanyRecord] = {}
        self._by_ticker: dict[str, set[int]] = {}
        self._by_name: dict[str, set[int]] = {}
        self._by_alias: dict[str, set[int]] = {}
        for r in records:
            self.records[r.cik] = r
            for t in [r.ticker, *r.tickers]:
                if t:
                    self._by_ticker.setdefault(normalize_ticker(t), set()).add(r.cik)
            key = normalize_name(r.name)
            if key:
                self._by_name.setdefault(key, set()).add(r.cik)
        for alias, cik in aliases:
            if cik not in self.records:
                raise ValueError(f"alias {alias!r} points at CIK {cik}, which is not in the index")
            key = normalize_name(alias)
            if key:
                self._by_alias.setdefault(key, set()).add(cik)
        self._names = list(self._by_name)

    @classmethod
    def from_csv(cls, companies: Path, aliases: Path = ALIASES_PATH) -> CompanyIndex:
        """``companies``: columns cik, name, ticker, aliases (extra tickers, ``|``-separated)."""
        with companies.open(newline="") as fh:
            records = [
                CompanyRecord(
                    cik=int(row["cik"]),
                    name=row["name"],
                    ticker=row["ticker"] or None,
                    tickers=[t for t in row["aliases"].split("|") if t],
                )
                for row in csv.DictReader(fh)
            ]
        return cls(records, load_aliases(aliases))

    @classmethod
    def from_connection(
        cls,
        conn: psycopg.Connection[Any],
        ciks: Sequence[int],
        aliases: Path = ALIASES_PATH,
    ) -> CompanyIndex:
        """The serving ``companies`` table restricted to ``ciks`` (the tracked set). Its
        ``aliases`` array holds the company's other tickers/share classes."""
        rows = conn.execute(
            "SELECT cik, name, ticker, aliases FROM companies WHERE cik = ANY(%s) ORDER BY cik",
            (list(ciks),),
        ).fetchall()
        records = [
            CompanyRecord(cik=r[0], name=r[1], ticker=r[2], tickers=list(r[3] or [])) for r in rows
        ]
        return cls(
            records, [(a, c) for a, c in load_aliases(aliases) if c in {r.cik for r in records}]
        )

    def _match(self, cik: int, how: MatchKind, score: float) -> CompanyMatch:
        r = self.records[cik]
        return CompanyMatch(cik=cik, name=r.name, ticker=r.ticker, how=how, score=score)

    def resolve(self, mention: str) -> list[CompanyMatch]:
        text = mention.strip()
        if not text:
            return []
        found = self._resolve_text(text)
        if found or "(" not in text:
            return found
        # "Block (formerly Square)": a bracketed note is a gloss, so try the name without it.
        # Only the name outside the brackets is ever matched, never what is inside them.
        outside = _BRACKETED.sub(" ", text).strip()
        return self._resolve_text(outside) if outside else []

    def _resolve_text(self, text: str) -> list[CompanyMatch]:
        # 1. exact ticker. Only a mention written as a ticker counts (ALL CAPS, or with a share
        #    class dot or dash, "BRK.b"): "all", "key", "now" are words, not Allstate or KeyCorp.
        bare = text.lstrip("$")
        if _TICKER_SHAPE.fullmatch(bare) and (bare == bare.upper() or re.search(r"[.-]", bare)):
            key = normalize_ticker(bare)
            if key in self._by_ticker:
                return self._sorted([self._match(c, "ticker", 100.0) for c in self._by_ticker[key]])
        variants = name_variants(text)
        # 2. exact normalized name, 3. alias
        for v in variants:
            if v in self._by_name:
                return self._sorted([self._match(c, "name", 100.0) for c in self._by_name[v]])
        for v in variants:
            if v in self._by_alias:
                return self._sorted([self._match(c, "alias", 100.0) for c in self._by_alias[v]])
        # 4. strict fuzzy: a one-word typo, or the start of one longer name
        best: dict[int, float] = {}
        for v in variants:
            for name in self._names:
                if _is_typo(v, name):
                    score = float(fuzz.ratio(v, name))
                elif _is_prefix(v, name):
                    score = float(fuzz.token_set_ratio(v, name))
                else:
                    continue
                for cik in self._by_name[name]:
                    best[cik] = max(best.get(cik, 0.0), score)
        return self._sorted([self._match(c, "fuzzy", s) for c, s in best.items()])

    def suggest(self, mention: str, limit: int = 5) -> list[CompanyMatch]:
        """Unguarded fuzzy candidates, for the clarification question only (never a resolution)."""
        best: dict[int, float] = {}
        for v in name_variants(mention):
            for name, score in self._fuzzy_candidates(v):
                for cik in self._by_name[name]:
                    best[cik] = max(best.get(cik, 0.0), score)
        return self._sorted([self._match(c, "fuzzy", s) for c, s in best.items()])[:limit]

    def _fuzzy_candidates(self, norm: str) -> list[tuple[str, float]]:
        if len(norm) < MIN_FUZZY_LEN:
            return []
        return [
            (name, float(score))
            for name, score, _ in process.extract(
                norm,
                self._names,
                scorer=fuzz.token_set_ratio,
                score_cutoff=FUZZY_CUTOFF,
                limit=None,
            )
        ]

    @staticmethod
    def _sorted(matches: list[CompanyMatch]) -> list[CompanyMatch]:
        return sorted(matches, key=lambda m: (-m.score, m.cik))


def _is_typo(norm: str, name: str) -> bool:
    """Same words as ``name`` but one, which is a single edit away (insert, delete, substitute or
    swap two letters) and at least ``MIN_TYPO_WORD`` letters long. Never an exact match (that is
    an earlier stage)."""
    words, name_words = norm.split(), name.split()
    if len(words) != len(name_words) or norm == name:
        return False
    differing = [(w, n) for w, n in zip(words, name_words, strict=True) if w != n]
    if len(differing) != 1:
        return False
    w, n = differing[0]
    return min(len(w), len(n)) >= MIN_TYPO_WORD and OSA.distance(w, n) == 1


def _is_prefix(norm: str, name: str) -> bool:
    """A multi-word mention that starts a strictly longer name."""
    words, name_words = norm.split(), name.split()
    return len(words) >= 2 and len(name_words) > len(words) and name_words[: len(words)] == words


def load_aliases(path: Path = ALIASES_PATH) -> list[tuple[str, int]]:
    with path.open(newline="") as fh:
        return [(row["alias"], int(row["cik"])) for row in csv.DictReader(fh)]


def resolve_company(mention: str, index: CompanyIndex) -> list[CompanyMatch]:
    """Matches for ``mention``: one = resolved, several = ambiguous, none = unknown."""
    return index.resolve(mention)


def clarification_for(mention: str, matches: Sequence[CompanyMatch]) -> str:
    """The question to put to the user when ``matches`` (resolved or suggested) is not exactly
    one company the mention clearly names."""
    if not matches:
        return (
            f"I could not find a tracked company matching '{mention}'. Which company do you mean?"
        )
    options = ", ".join(f"{m.name} ({m.ticker})" if m.ticker else m.name for m in matches[:5])
    if len(matches) == 1:
        return f"I did not recognize '{mention}' exactly. Did you mean {options}?"
    return f"'{mention}' could be several companies: {options}. Which one do you mean?"
