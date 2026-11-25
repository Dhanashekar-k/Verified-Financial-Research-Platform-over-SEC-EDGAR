# Judge Calibration — Labeling Task

**One line:** Prove the LLM "judge" grades FinanceBench answers the way a human would, by grading the same 46 answers yourself (blind) and computing the agreement (Cohen's κ).

---

## Why this task exists

The eval harness scores FinanceBench by asking an LLM (the **judge**, Sonnet 5.5) whether the system's answer matches the gold answer. An LLM grading an LLM is circular — the number means nothing until you show the judge agrees with a human. This task is that evidence.

Right now the baseline's FinanceBench accuracy reads **24%**, but `judge_kappa` is null, so that 24% is just the judge's opinion, not a measurement. Your project's whole claim is *"verified."* κ is what makes the FinanceBench numbers real.

## End goal

- `eval/judges/calibration.jsonl` — your correct/incorrect label for each of the 46 answered FinanceBench rows.
- A **Cohen's κ** (you vs the judge) reported next to every FinanceBench number.
- **Target κ ≥ 0.70.** At/above → the judge is a trustworthy proxy, its numbers stand. Below → the judge rubric needs fixing (last section).

## What you are actually doing

For each of 46 rows you see three things:

- **Question** — a FinanceBench question about a company's filing.
- **Gold answer** — the human-authored correct answer from the FinanceBench benchmark. **Treat this as ground truth.**
- **Baseline answer** — what the system produced.

You decide one thing: **does the baseline answer match the gold answer — correct or incorrect?**

You are **not** re-answering the question, reading the filing, or fixing anything. You are grading one existing answer against a known-correct answer.

## The decision rule

**CORRECT** — the baseline reaches the same substantive conclusion as the gold:
- The key figure or yes/no matches the gold. Wording and extra correct detail don't matter.
- A number that rounds to the gold or is within ~1% counts (gold $831M vs "about $831 million" or $830M → correct).

**INCORRECT** — any of:
- Wrong number, wrong sign/direction, or wrong magnitude (millions vs billions).
- Contradicts the gold, or answers a different question than was asked.
- Makes a claim the gold doesn't support.
- Hedges so heavily it never actually answers ("it depends," "cannot determine") — even if the harness logged it as answered.

### Edge cases — decide these the same way every time

| Situation | Label |
|---|---|
| Right core number, weak/wrong explanation | **Correct** (the question asks for the number) |
| Right explanation, wrong number | **Incorrect** (the number is the answer) |
| Number off only by rounding / within ~1% | **Correct** |
| Number off by more than a rounding error | **Incorrect** |
| Correct answer **plus** an added claim that's materially wrong | **Incorrect** |
| "Can't determine" / non-answer that was logged as answered | **Incorrect** |
| Gold answer itself is ambiguous | Best reading, then stay consistent |

The rule that outranks every single call above: **apply the same standard to all 46.** κ measures whether the judge draws the line where you draw it — so your line has to stay in one place.

## The golden principle: blind and consistent

- **Blind** — you never see the judge's label before you decide. The tool enforces this. If you anchored on the judge, κ would be meaningless.
- **Consistent** — one standard, first row to last. Not lenient, not harsh — the standard a careful analyst would use.

You can hand the labeling to someone else, but it should be someone comfortable reading a financial answer and telling whether it matches a known-correct one. They don't need to know the codebase.

## How to run it

In a Terminal, in the repo:

```
make label-calibration
```

Per row: **c** = correct, **i** = incorrect, **skip**, **b** = back one row, **v** = review from row 1. At the end a confirm prompt appears: **y** finishes, **b** returns to the last row, **v** reviews from the top. **q** quits mid-session without finishing (just rerun to resume). A mistyped key re-shows the row.

When you confirm, it prints a **promote command** (it writes `eval/judges/calibration.jsonl`, which is gold-protected, so you run that line yourself). Then:

```
make kappa RESULTS=eval/results/2026-11-25T00-43-10Z_baseline-0.2.0_sonnet.json
```

That writes `kappa.json` and prints κ.

## Reading the result

| κ | Meaning |
|---|---|
| ≥ 0.80 | Strong — judge is a reliable human proxy |
| 0.70 – 0.80 | Substantial — meets the bar; report it |
| 0.60 – 0.70 | Marginal — usable with caveats; consider refining |
| < 0.60 | Weak — fix the judge rubric before its numbers mean anything |

Always report κ **with n (46) and where the judge disagreed** — e.g. *"κ = 0.74 (n = 46); the judge over-credited 3 partially-correct numeric answers."* That sentence is a strong interview answer on its own.

## If κ is below target

You fix the **judge**, not your labels. Your labels are the human ground truth — they do not change.

1. Look at the rows where you and the judge disagree (`kappa.json` / per-question data).
2. Find the pattern (e.g. the judge counts a partially-right answer as correct, or treats a hedge as an answer).
3. Tighten the judge's rubric in `eval/judges/correctness.py` to close that gap.
4. Re-run the judge over the same answers; recompute κ against your same labels.

Repeat until κ clears the bar, or until you've documented why it won't.

## What this is NOT

- Not re-answering the questions.
- Not reading the filings yourself.
- Not fixing or improving the baseline.
- Not editing the gold answers.

Just grading 46 existing answers against a known-correct answer — consistently, and blind to the judge.
