# M6: open spec questions for the cores (Shekar decides)

The stubs' docstrings are the spec, and the hook now protects the three core stubs. These gaps were found while writing their tests (2026-11-26, mostly by the `test-writer` agent). The tests leave them unpinned unless a row says otherwise. "Suggestion" is only a suggestion.

**Implement `numbers.py` first.** The other suites also wait on `numbers.IMPLEMENTED`. If `policy.py` is finished before it, some tests pass unexpectedly and strict xfail reports them as failures.

## Decide these first: as written, a wrong claim can pass or a right one can fail

| # | Question | Concrete case | Suggestion |
|---|---|---|---|
| A | **How is a company recognised in text?** The spec says "by name, ticker or alias". Real records hold `MICROSOFT CORP` / `MSFT` with no alias | "Microsoft's FY2023 revenue was ..." over Apple's fact **passes** unless suffixes are ignored. Tickers that are also words (`A`, `ALL`, `IT`, `ON`, `NOW`) cause false drops. **No test covers real-record matching** | Match names normalized with `resolve_company.normalize_name`. Match tickers only in capitals, and skip one-letter and common-word tickers. Add a real-record test |
| B | **May a quote cut through a number?** A plain substring rule allows it | Quote "net sales were $298" cut from "$298,085 million" supports the claim "Net sales were $298." Likewise "6%" cut from "16%" | A match may not start or end inside a number |
| C | **January fiscal year ends.** The year of `period_end` is allowed | "In **fiscal** 2024, revenue was ..." passes for fiscal **2023** data ending 2024-01-31, which is a wrong-year miss | Allow the `period_end` year only inside a date, never after "fiscal" or "FY" |
| D | **`claim.unit` is never checked** | An adversarial row that changes only `unit` would be missed | Check it, or don't write unit-only rows |
| E | **D4 scale has a measured cost on dev** | A count over the trace and the Gate 0 verdicts, **not** a verifier measurement (`gate0_m5_grounding.json`, `d4_scale_conflict`): six text claims the judge supported write a scale word over a bare table cell (`fb-00438 c2`, `fb-00517 c1`/`c2`/`c3`, `fb-00711 c1`, `fb-04171 c1`) | (a) Keep D4 and accept the drops. (b) Accept equal digits: this also lets "6,098 **billion**" through. **(c)** Accept when the cited passage states the matching scale ("in millions"). I lean to (c). The tests hold under (a) and (c) |
| F | **Which other numbers count as "supported" in a numeric claim?** | "grew 4.8% to $383.3 billion" (a cited input); "later restated to $390.0 billion" (composer rule 5 asks for this); "ranked 2nd", "52-week" | Allow `v*`, each cited fact's value and the restated note's `latest_value`. Ordinals and "52-week" are labels |
| G | **Direction and sign rules can drop true claims** | "Microsoft's revenue was $171.4 billion lower than Apple's" (Apple minus Microsoft is positive); "Interest on higher debt decreased 36.7%" (two direction words); "(6)%" in a quote against "a 6% decrease" | Bind the direction word to the number it describes; in quotes, treat a decline word like a minus sign |
| H0 | **Composer wording can still reach the user.** D6 keeps a composer refusal's `reason` and `missing` unless they add an unsupported number, and the renderer shows `reason` first | A composer reason that paraphrases a dropped claim without a number passes. `fb-00283`'s reason opens by asserting what the passages report. The pipeline checks `found` only | **Always template `reason`** and build `missing` from slots. It costs some wording; it closes the leak |
| H1 | **A text claim's `value`, `display` and `unit` are never checked**, and kept claims pass through unchanged | Clean text and a real quote with `display: "$999 billion"` stays in `Answer.claims`. No dev text claim carries either today | Fail `number_in_text` when a text claim has a `value` or `display` |

**Pipeline rule to code against:** a kept claim needs `pass` on all three checks of its kind. "Nothing to object to" (no company restriction, no number in the claim) is `pass`, not `n/a`, or the pipeline raises.

## Define when you get there

| # | Question | Suggestion |
|---|---|---|
| H | Refusal: `found` lists every kept text, yet no dropped text may appear. That is impossible if a dropped claim has the same text as a kept one | Read it as: no text that only a dropped claim has. The pipeline enforces `found == kept texts` |
| I | A numeric plan with no results, no `not_found`, `text_only=False` and one kept text claim: verified? | Refuse |
| J | Refusal `missing`: are verifier entries added to the composer's? Does one bad entry remove itself or the whole list? What goes there when only the primary claim failed? | Append; remove just the bad entry; name the primary claim's metric and period |
| K | A restated-note claim on a `difference` result: the direction rule and "either company may be named" still apply to one raw fact | Require the fact's own company and skip direction |
| L | Forms not listed: "24 percent", "25 basis points", "percentage points", the century of `FY99`; "(3.1%)" as an aside and "(1)" as a footnote read as negatives | Decide in `numbers.py` and add cases |
| M | Year rule: `2000 bps`, `2050 shares` and "2050 stores" count as years and are never checked | Also exclude numbers followed by a unit word |
| N | A scale header **inside** the quote ("(in millions) Net sales 32,502"), and "(in thousands)" written in a claim (`fb-03282 c1` must still pass) | Decide together with E |
| O | `locate_quote`: case folding can lengthen text (`Straße`), and surrounding spaces make "the first span" ambiguous | Return the smallest stripped span |

## Allowed by D1 as agreed, so expect it
A **display** of "$385.0 billion" passes for 383,285,000,000 (within 0.5%), while the same figure in claim **text** fails. An adversarial row that changes only `display` can therefore be missed.

## Judgment calls the tests already pin (change them if you disagree)
A zero divisor fails the claim instead of raising. The alias "Chase" does not match inside "purchases". `COVID-19` is a label. Results align with claims by position. Composer `reason` or `missing` text that copies a dropped claim is removed. A `missing` entry for a period names the year.
