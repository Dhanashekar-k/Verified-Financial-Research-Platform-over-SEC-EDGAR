# Section-boundary gold labels: how to fill `sections_gold_template.jsonl`

Goal: for 10 real 10-Ks, say where Items 1, 1A, 7, 7A and 8 **really** start (n = 50, half the
runbook's 100: a deliberate deviation, see `docs/milestones/M02-report.md` item 2b). The parser is
graded against your answers, so nothing here shows or uses the parser's own section assignments.

## What is in the template

One row per (filing, item): 10 filings x 5 items = 50 rows. A row holds:

| Field | Who fills it | Meaning |
|---|---|---|
| `accession`, `section`, `text_file`, `candidates` | prepared | `candidates` are plain regex hits (`Item <n>[A-C]?` at the start of a line) with their character `offset` and the lines around them. A hit can be a table-of-contents line, a cross-reference or the real heading: that is for you to decide. |
| `present` | you (via the tool) | `true` if the filing has that section, `false` if not. |
| `true_start_offset` | you (via the tool) | Character offset, in the text file, of the line where the section really begins (its heading line). |
| `first_sentence` | the `fill` step proposes, you confirm | First sentence of the section's first body line of 40+ characters after the heading (short sub-headings such as "Overview" are skipped on purpose). |
| `labeled_by` | the tool | Your name (`--by`). |
| `notes` | you, optional | Anything odd, e.g. "heading split over two lines". |

The text files are `data/fixture_candidates/labeling/<ticker>_<accession>.txt` (scratch, gitignored;
the offsets index into them, and `candidates[].offset` was checked against them).

## The real start of a section

The **heading line of the body**, not the table of contents (a TOC line is followed at once by the
next item, and often carries a page number) and not a sentence that merely mentions the item
("see Item 1A"). If a filing prints the heading twice in a row (a page header, then the heading),
take the first. If the section exists but no candidate is its heading, use `manual`.

## Steps

1. `make label-sections` (resumes where you stopped; every answer is saved at once). For each item it
   lists the candidates as numbers with the text around them. Answer with:
   - `<number>`: that candidate is the real start;
   - `absent`: the filing has no such section (Rollins FY2020 has no `Item 1A` hit at all);
   - `manual`: no candidate is right; give a character offset or `line N`;
   - `show <n>` for a longer look, `n` / `p` for the next/previous page, `skip`, `back`, `quit`, `?`.
2. `uv run python -m eval.labeling_harness fill` proposes `first_sentence` for every confirmed row.
   **Read each one.** Retype it if it is not the first sentence of the section's text.
3. `uv run python -m eval.labeling_harness check` must say `all 50 rows look ready` (rows with
   `present: false` are reported: they are dropped in step 4, so n can end below 50).
4. Promote (the hook blocks me from writing `eval/sets/`, so you run this):

   ```bash
   uv run python - <<'EOF'
   import json
   rows = [json.loads(l) for l in open("eval/sets/drafts/sections_gold_template.jsonl")]
   keep = [{k: r[k] for k in ("accession", "section", "first_sentence", "labeled_by")}
           for r in rows if r["present"]]
   open("eval/sets/sections_gold.jsonl", "w").write(
       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
   print(len(keep), "rows")
   EOF
   make gold-checksums
   ```

5. Score it: `uv run python -m eval.sections_eval --min-accuracy 0.9`. It prints accuracy per item and
   overall against the 90% target, says it rests on n labels (runbook: 100), lists every miss with the
   gold start next to the parser's start, and exits non-zero below the target.

## Read the result as what it is

The 10 filings are a **stratified sample** (2 opaque-id TOC, 2 named-id TOC, 2 no-anchor TOC,
2 multi-registrant, 2 where the parser is known to miss an item), not a random one, and two were
picked because the parser fails on them. Its accuracy is not an estimate for the whole corpus.
