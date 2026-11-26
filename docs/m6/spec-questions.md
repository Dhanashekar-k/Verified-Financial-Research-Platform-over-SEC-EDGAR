# M6: spec status for the cores

**Questions A to O, H0 and H1 are decided** (2026-11-26). The decisions, and how conflicts among them were read, are in `docs/decisions.md`, ADR-049, "Spec decisions". The tests pin them. **Where a stub docstring disagrees, ADR-049 wins** (docstrings were not edited).

**Implement `numbers.py` first.** The other suites also wait on `numbers.IMPLEMENTED`. If `policy.py` is finished before it, some tests pass unexpectedly and strict xfail reports them as failures.

## Docstring sentences that are now out of date
| File | The docstring says | Now |
|---|---|---|
| `numbers.py` | A year in a date is a `year`; a four-digit integer is a year unless marked by `$`, a comma, a point, `%` or a scale word | Inside a date it is a `label` (C). A unit word also stops it being a year (M). Ordinals and "52-week" are labels (F) |
| `numbers.py` | `policy.py` calls it for numbers in a composer's refusal | H0 never shows composer text, so `policy.py` may not need it. Its tests still wait on `numbers.IMPLEMENTED` |
| `numeric.py` | A year may be the `period_end` year; "lower" requires a negative result; no `unit` check; no list of other supported numbers | "fiscal YYYY" is exact (C); relational phrasing is kept (G); `unit` is checked (D); supported numbers are listed (F) |
| `quotes.py` | A number is matched within the claim's own precision; a bare table number never supports a scaled claim; sign counts; no company or `value` rule | Digit sequence plus a declared scale (E); decline wording reads as negative (G); cut numbers fail (B); company guard (A); no `value` or `display` (H1) |
| `policy.py` | A composer's clean `reason` and `missing` stay | Always templated; `missing` from slots (H0). I and J added |

## Still open: not pinned, and some let a wrong claim pass
| # | Gap | Concrete case | Suggestion |
|---|---|---|---|
| 1 | **E ignores the decimal point if "digit sequence" is read literally** | "grew **5.5**%" over a quote that says "**55**%" | Compare the digits **with** the decimal point's position, after removing thousands separators. All six accept cases still pass |
| 2 | **E does not compare `%` or currency.** An older test that dropped "decreased **3**" over "3**%**" was removed, since E does not say | A count supported by a percentage | A percent on one side requires a percent on the other |
| 3 | **E and a scale written beside the number** | Quote "$5.5 **billion**", passage header "(in millions)", claim "$5.5 **million**" passes by the letter of E | The number's own scale word wins over the header |
| 4 | **D with no unit** | `num-0213 c1` has `unit: null` (1 of 102 numeric dev claims). "Checked exactly" would drop it | Decide; no test either way. A `USD` label on a per-share fact is also unpinned |
| 5 | **A: what counts as a mention** | One-word company names are also ordinary words ("Target", "Block"). Which tickers are "common English words" is your list | Require capitalization in the claim; keep an explicit ticker stop-list. Exchange names must be excluded (`fb-00746 c1`) |
| 6 | **A on text claims has a known cost** | `fb-02419 c1`, judge-supported, says Pfizer's unit combined with Mylan "to form Viatris", over a Pfizer passage. Viatris is tracked, so the guard drops it | Accept it, or limit the text guard to the claim's subject. To apply A to numeric claims only, delete the "A: a company named in a text claim" tests in `test_quotes.py` |
| 7 | **C: forms not pinned** (that "fiscal 2023 shares" stays a year is pinned) | "fiscal **year** 2023"; whether "October 2023" is a date; a bare "In 2024" for a January year end still follows the older `period_end` rule | Treat "fiscal year YYYY" like "fiscal YYYY" |
| 8 | **M: which unit words** | Pinned: `bps`, "basis points", `shares`. "2050 stores" is not pinned, and "In 2022 stores closed" must stay a year | Keep the list short |
| 9 | **B does not mention a sign or `%`** | Quote "3% this year" cut from "-3% this year" supports "changed by 3%": a sign flip kept. Likewise "grew 16" cut from "16%" | Treat a leading minus or parenthesis and a trailing `%` as part of the numeric token |
| 10 | **E, a claim with no scale word** | "Net sales were $55,893." over "$55,893 million" passes by the letter of E: wrong by a factor of a million | When the quote or the passage declares a scale, require the claim to state it for currency amounts |
| 11 | **A: names no variant matches** | "Domino's" may not match `DOMINOS PIZZA INC` (the filed name carries "Pizza", and I found no alias) | Add the brand to `app/engine/entities/aliases.csv` as you find them |

## Follow-up outside your files (Claude's, not done in this batch)
- `render.py`'s docstring still says a composer's refusal reason can be shown. H0 ends that.
- `pipeline.py` does not enforce H0. It could reject a refusal whose reason or `missing` copies composer text, as it already checks `found`.

## Judgment calls the tests pin (change them if you disagree)
- A zero divisor fails the claim instead of raising.
- The alias "Chase" does not match inside "purchases". `COVID-19` is a label.
- Results align with claims by position.
- A `missing` entry for a period names the year.
- **D** fails `metadata_match`, **B** fails `quote_substring`, and **H1**, **E** and a quote contradiction under **G** fail `number_in_text`. A claimed direction against the result's sign (**G**, numeric) fails `metadata_match`.
- **H0:** two different composer refusals over the same claims give the identical refusal.
