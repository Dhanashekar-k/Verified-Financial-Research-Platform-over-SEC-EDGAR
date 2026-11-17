# 06: Claude Code playbook for this project

Goal: Claude Code does the volume (boilerplate, infrastructure, tests, UI) while you keep ownership of the parts an interviewer will probe. Claude Code features named here were checked against the docs on 2026-11-16 (`code.claude.com/docs`). If something behaves differently, the docs win.

---

## 1. Setup, and the billing trap (do this first)

1. Install Claude Code using the method on the docs' install page, then sign in with your **Max** subscription (`/login`).
2. **The trap:** if an `ANTHROPIC_API_KEY` environment variable is present, in your shell profile *or picked up from a project `.env`*, Claude Code authenticates with that key and **bills your API credits instead of your subscription** [Certain]. In non-interactive mode (`claude -p`) a present key is always used [Likely]. Several users have reported burning $50+ this way.
   - Never define `ANTHROPIC_API_KEY` anywhere for this project. The app reads **`EDGARQA_ANTHROPIC_API_KEY`** and passes it explicitly: `anthropic.Anthropic(api_key=settings.edgarqa_anthropic_api_key)`.
   - Add `unset ANTHROPIC_API_KEY` to `~/.zshrc` as a belt-and-braces measure.
   - Run `/status` at the start of every session and check that it shows the subscription.
3. Install `jq` (`brew install jq`). The hooks use it.
4. In the repo (M0): copy `manual/repo_starter/` into the repo root, **rename `dot-claude/` to `.claude/` and `Makefile.starter` to `Makefile`** (they're shipped under neutral names because the transfer tool can't write those paths), then run `chmod +x .claude/hooks/*.sh`. Copy `manual/0*.md` and `manual/milestones/*.md` into `docs/plan/`, so the skills and `CLAUDE.md` can reference them (for example `docs/plan/05_data_contracts.md`).
5. Start `claude` in the repo root and run `/memory`. Check that `CLAUDE.md` is loaded.

## 2. What's in `.claude/` and why

| Path | Type | What it does |
|---|---|---|
| `CLAUDE.md` | Project memory | Spec summary, conventions, hard rules (never invent numbers, never edit gold sets, the stub LLM in tests, the key name). Under 200 lines, as the docs recommend [Certain] |
| `.claude/settings.json` | Settings | Permissions (allow safe commands; **ask** for terraform/aws mutations and `git push`; **deny** reading `.env`/keys) and hook registration |
| `.claude/hooks/protect-paths.sh` | PreToolUse hook (Edit/Write) | Blocks Claude from editing (a) the **core modules you write yourself**, (b) **gold eval files**, (c) `.env*` and Terraform state. Exit code 2 blocks the edit and tells Claude why [Certain] |
| `.claude/hooks/format-python.sh` | PostToolUse hook (Edit/Write) | Runs `ruff format` + `ruff check --fix` on edited `.py` files |
| `.claude/rules/core-modules.md` | Path-scoped rule (`paths:` frontmatter) | When Claude touches `app/engine/{plan,verify,router,sql_guard}`, it reviews, explains and writes tests, but doesn't implement |
| `.claude/rules/data-pipelines.md` | Path-scoped rule | SEC client rules, idempotency, Iceberg conventions, no real AWS calls in tests |
| `.claude/rules/eval.md` | Path-scoped rule | Results only come from runs; bootstrap CIs; never edit gold |
| `.claude/skills/milestone/` | Skill `/milestone M5` | Loads `docs/plan/M05_*.md`, restates inputs/outputs/DoD, proposes a plan, lists the files it will touch and the files you own |
| `.claude/skills/dod/` | Skill `/dod M5` | Runs each DoD command, records pass/fail and copies numbers **only from result files** into `docs/milestones/M05-report.md` |
| `.claude/skills/interview-me/` | Skill `/interview-me <path>` | Skeptical-interviewer drill on a module; scores your answers; lists what to re-study |
| `.claude/skills/adr/` | Skill `/adr <title>` | Appends a numbered ADR (context, options, decision, consequences, how to revisit) |
| `.claude/skills/results-audit/` | Skill `/results-audit` | Checks that every number in `docs/results.md` and `README.md` traces to `eval/results/*.json` |
| `.claude/agents/reviewer.md` | Subagent (read-only tools) | Reviews a diff against `CLAUDE.md` rules and the milestone contract |
| `.claude/agents/test-writer.md` | Subagent | Writes failing tests from a spec or contract before the implementation exists |
| `.claude/agents/numbers-auditor.md` | Subagent (read-only) | Used by `/results-audit` |

## 3. The session loop (every milestone)

```
claude                         # in repo root
/status                        # check: subscription, not API key
/milestone M3                  # restates the contract, proposes a plan (stay in plan mode)
  → you edit and approve the plan
  → Claude implements step 1 → runs tests → you review the diff → commit
  → repeat per step (small commits; one concern per commit)
/dod M3                        # runs the DoD and writes docs/milestones/M03-report.md
/interview-me serving/load     # drill; anything you fail → fix your understanding or the code
/adr "Atomic corpus swap via views"   # if a decision was made
/clear                         # fresh context for the next milestone
```

**Plan mode first, always.** Cycle permission modes with Shift+Tab until it shows plan mode [Likely; see "Plan before editing" in the Common workflows docs]. Have it produce the plan, then let it edit. Most wasted Claude Code time comes from letting it code before the plan is right.

**Keep context clean.** Don't paste large data. Ask it to `head -n 20` a file. Use `/compact` when a session gets long, and `/clear` between milestones. The milestone runbook carries the context, so you don't need to re-explain.

**Undo.** Claude Code checkpoints file edits, so you can rewind a bad turn (see "Undo changes with checkpoints" in *How Claude Code works*) [Likely]. Git is still the real safety net, so commit often.

## 4. Division of labor (be strict about this; it's the interview)

| Claude Code writes (you review) | You write (Claude reviews, explains, writes tests) |
|---|---|
| Makefile, docker-compose, pyproject, CI workflows | `app/engine/plan/compiler.py`, `periods.py`, `metric_catalog.yaml` (the first 20 metrics) |
| Terraform modules | `app/engine/verify/numeric.py`, `quotes.py`, `policy.py` |
| SEC HTTP client (with your rate-limit spec), FSDS downloader, `normalize_facts.py` I/O scaffolding | `pipelines/spark/fiscal.py`, `versions.py`, `pipelines/common/fiscal_ref.py`: the fiscal-period and dedup logic |
| HTML parser/sectioner (you review it against 10 real filings) | `app/engine/sql_guard/guard.py` |
| Chunker, embedding job, export job, loader, `ml/router/train.py` scaffolding | `ml/router/model_fns.py` (`fit_model`, `evaluate_model`), `app/engine/router/cascade.py` |
| FastAPI scaffolding, auth, rate limiter, cache, tracing | `eval/run.py` metric definitions (accuracy, refusal, unsupported-claim rate) |
| k6 scripts, Next.js UI, Lambdas, the DAG | ADR text (Claude drafts; you rewrite in your own words) |
| Test scaffolding, fixtures, stub LLM | Hand labels: router test set, judge calibration, narrative-set verification |

**How it's enforced:** `protect-paths.sh` blocks Claude's Edit/Write on your files once they exist. Claude may create the initial stub, so scaffolding still works; I tested the hook against these paths. To have Claude apply a diff you've *already reviewed in chat*, start that one session as `EDGARQA_ALLOW_CORE_EDITS=1 claude`; the hook allows edits when that variable is set. Use it rarely, and say so honestly if asked ("I designed and wrote X; I used Claude Code for Y").

## 5. Prompts that work for this project

**Spec → tests → code (the default pattern)**
```
Read docs/plan/M06_verifier.md §4 and docs/... contracts §4.3–4.4.
Use the test-writer subagent to write failing tests in tests/unit/verify/ for numeric matching:
scale (thousands vs millions), rounding-aware tolerance from displayed precision, wrong fiscal year,
wrong unit, percent vs ratio, negative values, "N/A". Don't implement anything.
List the tests and the behavior each one pins down.
```

**Explain-back (after you've written a core module)**
```
Act as a skeptical senior engineer. Read app/engine/plan/compiler.py.
Ask me 8 questions, one at a time, that an interviewer would ask about it. After each answer,
tell me what was wrong or missing, citing line numbers. Don't rewrite the code.
```

**Diff review before commit**
```
Use the reviewer subagent on `git diff --staged`. Check against CLAUDE.md rules and the
M05 contract. Report only real problems (correctness, contract drift, missing tests, invented
numbers or tags). No style nits.
```

**Debugging an eval regression**
```
Compare eval/results/<old>.json and <new>.json. List the questions that flipped from correct
to wrong, grouped by route and by failure stage (route, plan, compile, retrieve, compose, verify),
using the trace IDs. Propose the 3 most likely causes, ranked, with the evidence for each.
Don't change code yet.
```

**Terraform with guardrails**
```
Write the Terraform module infra/terraform/modules/emr for an EMR Serverless Spark application
(arm64, release label from variables, auto-stop after 5 min idle, maximum capacity 64 vCPU / 256 GB),
plus an IAM job role with read/write on the lake bucket and Glue catalog access only.
Add default_tags. Run `terraform fmt` and `terraform validate` only. Don't plan or apply.
```

**Writing an ADR in your voice**
```
/adr "Typed query plan instead of text-to-SQL"
Draft it from our discussion and docs/plan/M05. Keep it under 250 words. Mark the
"Consequences" section TODO for me to write myself.
```

## 6. Things Claude Code tends to get wrong here (and the countermeasure)

| Failure | Countermeasure |
|---|---|
| **Invents XBRL tag names** that look plausible | Metric catalog tags must exist in `raw.fsds_tag`; a unit test checks every catalog tag against the tag table |
| **"Fixes" a failing eval by editing gold or loosening tolerance** | Gold files protected by the hook (Edit/Write) **and** by `make check-gold` in CI (checksums you generate with `make gold-checksums`, which catches shell edits the hook can't see); tolerance lives in the gold rows; the reviewer agent flags tolerance changes |
| **Mocks away the thing under test** (for example, stubs the verifier in a verifier test) | `CLAUDE.md` rule: only the LLM boundary and SEC HTTP may be stubbed in unit tests |
| **Real API calls from tests** (reported to burn credits) | The stub LLM is the default; a pytest fixture blocks sockets to `api.anthropic.com` and `sec.gov` unless marked `@pytest.mark.live` |
| **Silently changes a contract** (schema or JSON field names) | Contracts live in `docs/plan/05_data_contracts.md`; the reviewer agent checks drift; Pydantic models are the single source in code |
| **Over-engineers** (Kafka, Kubernetes, a service mesh, an abstract base class for everything) | `CLAUDE.md` "Do not add" list; plan mode first |
| **Wrong Spark/Iceberg versions or configs** | Versions pinned in `pipelines/common/config.py`; one `spark_session()` factory for local and EMR |
| **Runs `terraform apply` or mutating `aws` commands** | `ask` permissions; you approve every apply |
| **Reports numbers from memory** in docs | `/results-audit` before every commit that touches `docs/results.md` or `README.md` |

## 7. Parallel work (optional, from week 3)

- **Git worktrees** let one Claude session build the UI while you work on the engine in another: `git worktree add ../edgar-ui -b feat/ui`, then `cd ../edgar-ui && claude`. Claude Code also has built-in worktree support; see the Worktrees docs page [Certain that it exists; check the flags there].
- **Background subagents:** ask for "the test-writer subagent in the background" while you keep coding.
- Don't run two sessions that touch the same module.

## 8. Headless use (optional)

`claude -p "<prompt>" --output-format json` is handy for repeatable chores, for example a pre-push `/results-audit` [Likely; see the Headless docs]. **Headless mode always uses an `ANTHROPIC_API_KEY` if one is present**, which is one more reason never to define it.

## 9. Learning log

Keep `docs/learning-log.md`: after each `/interview-me`, write down the questions you missed and the corrected answer in your own words. Re-read it before interviews. It's your personal question bank, built from your own code.
