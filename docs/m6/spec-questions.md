# M6: spec status for the cores

**Questions A to O, H0 and H1 are decided** (2026-11-26). The decisions, and how conflicts among them were read, are in `docs/decisions.md`, ADR-049, "Spec decisions". The tests pin them. **Where a stub docstring disagrees, ADR-049 wins** (docstrings were not edited).

**Implement `numbers.py` first.** The other suites also wait on `numbers.IMPLEMENTED`. If `policy.py` is finished before it, some tests pass unexpectedly and strict xfail reports them as failures.

## Docstring sentences that are now out of date
| File | The docstring says | Now |
|---|---|---|
| `numbers.py` | A year in a date is a `year`; a four-digit integer is a year unless marked by `$`, a comma, a point, `%` or a scale word | Inside a date it is a `label` (C). A unit word also stops it being a year (M). Ordinals, "52-week" and "3M" are not quantities |
| `numbers.py` | `policy.py` calls it for numbers in a composer's refusal | `policy.py` no longer sees composer wording. Its tests still wait on `numbers.IMPLEMENTED` |
| `numeric.py` | A year may be the `period_end` year; "lower" simply requires a negative result; no `unit` check; no list of other supported numbers | "fiscal YYYY" is exact (C); comparisons follow the first-named company (second round); `unit` is checked and may be missing only on ratio metrics; supported numbers are listed (F) |
| `quotes.py` | Sign counts; a bare table number never supports a scaled claim | Values compared with the passage's declared scale applied; decline wording reads as negative (G); a quote may not cut a number or its sign (B); no `value` or `display` (H1). There is **no** company-name rule for text claims |
| `policy.py` | A composer's clean `reason` and `missing` stay | `apply` receives only that the composer refused. Reason templated, `missing` from slots (H0, I, J) |

## Still open: not pinned
| # | Gap | Concrete case | Suggestion |
|---|---|---|---|
| 1 | **Which table cells a declared scale applies to** (prose is never scaled) | In a table "in millions", a percent column without `%` (`fb-00711 c1`'s passage has "32.7"), a count or a share column would also be scaled. Per-share cells are pinned as unscaled (synthetically and by dev claims whose passages say "except per share", "except EPS" or "except share data") | Scale currency amounts only; read the declaration's "except ..." clause |
| 2 | **A trailing `%` can be cut** | Quote "grew 16" out of "grew 16%" (the signed-token rule names a leading sign or parenthesis only) | Treat a trailing `%` as part of the token |
| 3 | **A parenthesised aside** | Under the signed-token rule, a quote starting at "3%" inside "(3% of sales)" cuts the token, although that parenthesis is not a minus | Count `(` as a sign only when it closes directly after the number |
| 4 | **Currency against a count** | "$5" over "5 stores" | Require currency on both sides when the claim writes `$`, allowing table cells |
| 5 | **A: what counts as a mention** (numeric claims) | One-word names are ordinary words ("Target", "Block"); the ticker stop-list is yours; "Domino's" may not match `DOMINOS PIZZA INC` | Require capitalisation; add brands to `aliases.csv` as found |
| 6 | **Comparison words** | "exceeds", "above", "trails" are lenient by omission (`num-0224 c3`, `num-0238 c1` use "exceed") | Leave lenient unless the adversarial set shows a need |
| 7 | **D: a label that disagrees in kind** | A `USD` unit on a per-share fact | Decide |
| 8 | **C: forms** | "fiscal **year** 2023"; whether "October 2023" is a date; a bare "In 2024" for a January year end still follows the older `period_end` rule | Treat "fiscal year YYYY" like "fiscal YYYY" |
| 9 | **M: unit words** | Pinned: `bps`, "basis points", `shares`. "2050 stores" is not | Keep the list short; "In 2022 stores closed" must stay a year |

## To confirm
`fb-01328 c2` moved from "passes" to "dropped on `number_in_text`" because the newer scale rule requires it (ADR-049, second round). To reverse it you would have to say when a bare claim number may match a scaled table cell; "$32,502" and "32,502" over a table in millions are pinned as drops.

**A false-drop source to watch:** composer rule 3 asks for a passage number "exactly as it appears in the quote", which for a table cell omits the scale.

## Judgment calls the tests pin (change them if you disagree)
- A zero divisor fails the claim instead of raising.
- The alias "Chase" does not match inside "purchases". `COVID-19` is a label.
- Results align with claims by position.
- A `missing` entry for a period names the year.
- B (cut numbers and signs) fails `quote_substring`; a missing or flipped unit, a wrong company and a false comparison fail `metadata_match`; H1, scale, value and a quote contradiction fail `number_in_text`.
- **H0:** two different composer refusals over the same claims give the identical refusal.
