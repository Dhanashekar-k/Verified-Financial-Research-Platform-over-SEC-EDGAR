# M11: Real users (the FDE story)

**Time:** about 14 h of your time over 10–14 calendar days (sessions in the weeks of Dec 7 and Dec 14; recruiting starts **now**) · **Roles:** FDE (main), AI engineer (the failure-driven eval flywheel) · **Depends on:** M9 alpha live, M8 security subset

## 1. Goal

At least 5 external testers, at least 100 logged questions, and at least 3 fixes shipped from their feedback, each with before/after numbers. Every observed failure becomes a regression test.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| Live alpha + invite codes (one per tester) | `docs/onboarding.md` (one page) |
| `usage_log`, `feedback`, traces | `docs/user-research/session-XX.md` (notes per session) |
| — | `make triage` CLI + labeled failure types |
| — | `eval/sets/regressions.jsonl` (you write; protected) |
| — | `CHANGELOG.md` entries for each fix; before/after tables in `docs/results.md` |
| — | `docs/user-research/summary.md`: who, what, failures, changes, next steps |

## 3. Recruiting (send it in week 2, Mon Nov 23)

**Who:** OSU student finance and investment clubs, Fisher College of Business master's/MBA students, classmates who've done finance internships, CFA candidates in your network, and LinkedIn contacts in equity research or FP&A. Aim to invite 15 to get 5–8 [Guessing at the response rate].

**Message template** (edit it to sound like you):
> Hi {name}, I'm building a research assistant that answers questions about public companies' SEC filings. Every number is checked against the XBRL data the company filed, and every sentence links to its source. Would you spend 30 minutes trying to break it (Zoom or in person) in the week of Dec 7 or Dec 14? I'm looking for people who read 10-Ks for class, clubs or work. You'd get early access and credit in the project write-up.
> *(Optional, once you have one: add a link to a 60-second screen recording.)*

Book sessions with a scheduling link and send the invite code and `onboarding.md` the day before.

## 4. Session protocol (30 minutes)

1. **(3 min) Consent and privacy:** questions are logged; don't enter personal information; you can ask for your data to be deleted.
2. **(15 min) Tasks, thinking aloud.** Don't help unless they're stuck for more than 2 minutes:
   - T1: "Find a number you'd normally look up in a 10-K for a company you follow."
   - T2: "Ask why something changed (a margin, revenue, a risk)."
   - T3: "Try to get it to say something wrong."
   - Then free exploration.
3. **(7 min) Debrief:** trust ("Would you put this number in a deck? Why or why not?"), what was confusing, what's missing, and three 1–5 ratings (usefulness, trust, speed).
4. **(5 min, after the session) Notes:** fill in `docs/user-research/session-XX.md` (template below) while it's fresh.

```markdown
# Session XX: {date}, {role/background, no names unless permitted}
## Questions asked (request_ids)
## Observed failures (what happened → what they expected → failure type)
## Trust moments (when they trusted or distrusted it, and why)
## Quotes (with permission)
## Ratings: usefulness _/5, trust _/5, speed _/5
## My top takeaway
```

## 5. Triage → regression flywheel

- `make triage SINCE=2026-12-07`: lists 👎 feedback, refused and partially verified answers, and slow (> p95) requests, each with question, route, verification summary and the Jaeger trace link.
- Label each with a **failure type**: `data` (missing or wrong facts), `mapping` (metric catalog), `entity`, `period`, `retrieval`, `generation`, `verification` (false drop or missed bad claim), `ux`, or `out_of_scope_ok` (a correct refusal the user disliked).
- Each real failure → a row in `eval/sets/regressions.jsonl` with the expected behavior (you write it).
- Pick the **top 3 by frequency × severity**. For each fix: write a regression test first, fix it, run `make bench SPLIT=dev` + regressions + test, and record a **before/after on the affected category** (paired bootstrap where n allows; otherwise report counts honestly), plus a `CHANGELOG.md` entry.
- Label the user questions for the router (M7): add them to `router_test.jsonl` as a new version and re-check the router's macro-F1 on real traffic.

## 6. Onboarding doc (`docs/onboarding.md`, one page)

What it does and doesn't do (no advice, no prices, US filers, 10-K/10-Q coverage dates); 6 example questions; how to read badges (verified / partially verified / refused); how to open sources; how to report a problem; the privacy note.

## 7. Claude Code session plan

```
/milestone M11
Build make triage (SQL over usage_log + feedback, with Jaeger trace URLs), a session-notes template
generator, and a script that turns my labeled triage CSV into regressions.jsonl candidates in
eval/sets/drafts/ (NOT the gold file. I'll move the rows I approve).
```
```
For fix #N: read the regression rows tagged {failure_type}, reproduce with the harness, and propose
the smallest fix with a failing test first. Don't touch protected files.
```

## 8. Definition of done (T11)

- [ ] ≥ 5 testers, ≥ 100 logged questions (from `usage_log`, excluding yours).
- [ ] ≥ 3 fixes shipped, each with a regression test and before/after numbers.
- [ ] `docs/user-research/summary.md` written: the failure-type distribution (a table), the top changes, what you'd do next, and 3 quotes if permitted.
- [ ] The router re-checked on user questions.

## 9. Pitfalls

- Helping testers during tasks (you learn nothing). Stay quiet and take notes.
- Fixing what's easy instead of what's frequent and severe.
- Counting your own questions toward the 100.
- Changing gold to match new behavior. Regressions encode *expected* behavior, decided before the fix.

## 10. Interview prep

- "Tell me about a time user feedback changed your design" → your best fix, with numbers.
- "A user says an answer is wrong. Walk me through the first hour" → trace → provenance → failure type → regression test → fix → communicate.
- "What surprised you about how people used it?"
