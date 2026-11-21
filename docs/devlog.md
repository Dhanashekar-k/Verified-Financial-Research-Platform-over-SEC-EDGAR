# Dev log

Running narrative, newest entry last. Heavy decisions live in `docs/decisions.md` (ADRs); this file holds
the day-to-day calls and progress. Append one dated entry at the end of each working session.

## 2026-11-21 — M2 complete
- **Done:** the full S&P 500 corpus (13,239 filings) is fetched and parsed on AWS Glue into the real `curated` tables (2.86M chunks, equal to `curated.filings.n_chunks`, 0 orphans). `curated.companies.in_financebench` now populates (32 companies).
- **Fixed:** the 10-Q part-state and implied-`P1-1` section bugs (ADR-031), and the O(n²) merge delete in `merge_curated` (anti-join instead of `NOT IN`), which had timed the first re-parse out.
- **Result:** parse success 88.5% strict / 99.06% lenient, closed on the DoD "or documented" branch (residual in ADR-028/031); section accuracy 49/50 = 98% (n=50, in-sample).
- **Decisions:** stopped chasing the parse-success residual and documented it instead; relaxed the coverage gate to "no filing loses body text" (heading lines moved into `section_title`), recorded in ADR-031.
- **Next:** M3 (embeddings + serving store), on Glue or local fastembed, since EMR is Org-blocked.
