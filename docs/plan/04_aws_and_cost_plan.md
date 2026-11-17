# 04: AWS account, cost plan and Claude API budget

Prices are us-east-1 list prices checked 2026-11-16. Usage amounts are estimates [Guessing] until you measure them. Re-check prices on the AWS pricing pages before big runs.

---

## 1. Which AWS account path you're on (decide on Day 0)

What changed [Certain]:
- Accounts opened **on or after July 15, 2025** pick a **Free plan** or a **Paid plan**. Both get **$100 in credits at sign-up and up to $100 more** for completing onboarding activities.
- The Free plan ends after **6 months or when credits run out, whichever comes first**. Free-plan accounts can't use services "that could deplete credits," and they upgrade to Paid automatically if you join AWS Organizations and in a few other cases.
- **You're ineligible for the Free plan and the credits if you have, or previously had, an AWS account.**
- Accounts opened **before July 15, 2025** stay on the legacy free tier. Its 12-month offers ran from the sign-up date, so a 2024 account's 12 months are over or nearly over.
- In AWS's new sign-up experience (rolling out since Sept 2026), **Amazon EMR is listed only under the Paid plan**, and classic **Amazon MWAA isn't supported**. **AWS Glue, Athena, Lambda, EC2, ECR, RDS/Aurora, S3, CloudFront, EventBridge, Step Functions, SQS, SageMaker and "Amazon MWAA Serverless"** are on the Free-tier list.
- Aurora PostgreSQL serverless is on the Free plan with **up to 4 ACUs and 1 GiB of storage per cluster**. That's too small for this project's chunk table.

```
Have you ever had an AWS account (any email, any time)?
├── YES → Use your existing account on pay-as-you-go. No new-customer credits.
│         Always-free offers still apply (Lambda, SQS, CloudFront, Glue Catalog, etc.).
│         Expected cash spend for the whole project: ~$45–80 [Guessing].
└── NO  → Create a new account and choose the PAID plan.
          Why Paid: EMR is Paid-only; the Free plan auto-closes at 6 months or $0 credits,
          which would kill the demo link on your resume.
          You still get the $100 (+ up to $100) credits, which should cover the project.
          Complete the credit-earning activities in week 1.
```

**If you're forced onto the Free plan** (for example, you won't add a card): swap EMR Serverless for **AWS Glue Spark jobs**. The same PySpark code works with Glue 5.1/6.0 and Iceberg [Likely]. Run Postgres on EC2, not Aurora's 1 GiB tier. Expect to rebuild everything when the 6 months end.

## 2. Guardrails (set them up before creating any resource; M0 runbook step 2)

1. **AWS Budgets:** monthly cost budget with alerts at $20, $50 and $80 (actual) and $80 (forecasted), sent to your email.
2. **Cost Anomaly Detection:** turn it on, with a daily email summary.
3. **Spend limit:** if your account shows the new "projects" experience, set a monthly spend limit there too [Unclear: depends on whether your account has it].
4. **Tag everything** `project=edgarqa`, `env=dev` (Terraform `default_tags`), and activate `project` as a cost allocation tag so Cost Explorer can group by it.
5. **No NAT gateway, ever.** It runs about $0.045/hour plus per-GB charges, or roughly $33+/month idle [Likely]. Lambdas run outside the VPC; EC2 sits in a public subnet with no inbound SSH.
6. **Public IPv4 costs $0.005/hour per address** [Certain], about $3.65/month. You'll have exactly one: the EC2 box. Release any Elastic IP you aren't using.
7. **EMR Serverless application:** set `auto_stop_configuration` (idle timeout of 5 min) and a **maximum capacity** (for example 64 vCPU / 256 GB) so a runaway job can't scale without limit.
8. **Athena workgroup:** a per-query bytes-scanned limit (for example 10 GB) and query results expiring after 7 days.
9. **CloudWatch Logs:** retention of 7 days on every log group (set it in Terraform).
10. **After every cloud session:** `make cost-today` (a Cost Explorer CLI query, filtered by tag) and `make status` (lists running EMR jobs, EC2 state, queue depths).

## 3. Service-by-service plan and cost

| Service | Use | Pricing basis | Estimated project cost |
|---|---|---|---|
| **S3** | Lake (raw FSDS, gzipped filing HTML, Iceberg tables), exports, UI, artifacts | About $0.023/GB-month Standard [Likely] + requests | Lake ~30–60 GB [Guessing] → **$1–2/month**; PUT requests a few dollars total |
| **Glue Data Catalog** | Iceberg catalog | Free up to 1M objects and 1M requests/month [Likely] | **$0** |
| **EMR Serverless** | `fsds_to_iceberg`, `normalize_facts`, `parse_filings`, `embed_chunks`, `export_serving` | $0.052624/vCPU-hour + $0.0057785/GB-hour, per second, 1-minute minimum; 20 GB of storage per worker included [Likely] | Facts rebuild about $1; parse about $2–3; embed about $1 [Guessing] → **$10–20 total** including dev runs |
| **Athena** | Ad-hoc SQL on the lake, DQ queries | About $5/TB scanned [Likely] | **< $1** |
| **EC2 t4g.medium** (2 vCPU, 4 GiB, Graviton) | Serving box: API + Postgres + Redis + Jaeger; also runs the one-time bulk fetch | $0.0336/hour [Likely] ≈ $24.5/month | About 4 weeks always-on → **~$25** |
| **EBS gp3 30 GB** | Postgres data and Docker images | About $0.08/GB-month [Likely] | **~$2.4/month** |
| **Public IPv4** | The EC2 box | $0.005/hour [Certain] | **~$3.65/month** |
| **CloudFront** | UI + `/api/*` proxy | Always-free 1 TB out and 10M requests/month [Likely] | **$0** |
| **Lambda** | discover, process_filing, merge, companyfacts | Always-free 1M requests + 400k GB-seconds/month [Likely] | **$0** |
| **SQS** | Per-filing fan-out + DLQ | 1M requests/month free [Likely] | **$0** |
| **EventBridge Scheduler** | Daily trigger | Large free allowance [Likely] | **$0** |
| **MWAA Serverless** | Daily DAG | About $0.08 per task-hour, 1-minute minimum per task [Likely, third-party Nov 2026] | About 5 tasks × 1–3 min/day → **< $1/month** |
| **Step Functions** (fallback) | Daily state machine | 4,000 state transitions/month free [Likely] | **$0** |
| **ECR** | arm64 images (API, Lambdas) | About $0.10/GB-month [Likely] | **< $1** |
| **SSM Parameter Store** | Secrets (standard SecureString) | Free for standard parameters [Likely] | **$0** |
| **CloudWatch** | Logs, 2–3 alarms | Logs ingestion per GB, alarms per month | **$1–3** |
| **Budgets / Cost Anomaly Detection** | Guardrails | Free at this usage [Likely] | **$0** |

**Total for the 6-week project: about $45–60, so plan for $80** [Guessing].

**After the project:** an always-on demo costs about $31/month (t4g.medium + EBS + IPv4). Three options:
1. **Keep it up during the job search.** Recruiter clicks are evidence. Probably worth it for 2–3 months [Likely].
2. **Demo on demand.** Stop the instance. A "Wake demo" button on the static UI calls a Lambda that starts it (about 1–2 min to boot) and an idle timer stops it again. While stopped you pay only for EBS, about $2.4/month, because an auto-assigned public IPv4 is released when the instance stops [Likely]; don't attach an Elastic IP. CloudFront needs a stable origin name, so the wake Lambda also updates the CloudFront origin to the new public DNS (or use a small Route 53 record) [Likely]. This makes a nice FDE-style cost story.
3. **Record the video, then `make destroy-compute`** and keep only the lake, budgets and IAM (S3 about $1/month). Rebuilding the demo then takes `terraform apply` + a deploy tag + `make remote-load` (about 30–45 min) [Guessing].

### Things that are *not* in this plan (and why)

| Avoided | Why | Cost avoided |
|---|---|---|
| Classic MWAA environment | Billed per hour for an always-on environment; not available in AWS's new sign-up experience [Certain] | Hundreds of $/month [Likely] |
| NAT gateway | See guardrail 5 | ~$33+/month |
| ALB | CloudFront → EC2 directly is enough for one origin | ~$16+/month [Likely] |
| ElastiCache / Aurora / RDS | Redis and Postgres run on the same box | ~$15–60/month |
| GPU instances | fastembed on CPU is fast enough for about 1M short chunks; new accounts often have **0 vCPU quota for G/P instances**, and quota requests take days [Likely] | — |
| App Runner | Reported closed to new customers from Apr 30, 2026 [Unclear: third-party report] | — |

## 4. Teardown and cost hygiene commands (built in M0, extended in M9)

| Make target | What it does |
|---|---|
| `make cost-today` | `aws ce get-cost-and-usage` for the last 2 days, grouped by service, filtered by `project=edgarqa` |
| `make status` | EMR job runs in RUNNING state, EC2 instance state, SQS depth and DLQ depth |
| `make stop-serving` | Stops the EC2 instance (data stays on EBS) |
| `make destroy-compute` | Terraform destroy of the serving, ingest and cdn modules; keeps lake, budgets and IAM |
| `make destroy-all` | Everything except the S3 lake bucket (needs `CONFIRM=yes`) |

## 5. Claude API budget

### Prices (per million tokens, checked 2026-11-16) [Certain]

| Model (API ID) | Input | Output | Cache hit | Batch (50% off) |
|---|---|---|---|---|
| Haiku 5.5 (`claude-haiku-5-5`), prompts ≤ 100K | $0.10 | $0.50 | $0.01 | $0.05 / $0.25 |
| Sonnet 5.5 (`claude-sonnet-5-5`) | $2 | $10 | $0.10 | $1 / $5 |
| Opus 5.5 (`claude-opus-5-5`) | $4 | $20 | $0.20 | $2 / $10 |

A 5-minute cache write costs 1.25× the base input price and pays for itself after one read [Certain]. Haiku 5.5's price rises above 100K-token prompts, so **keep every prompt well under 100K** [Certain].

### Per-question estimate (token counts are [Guessing]; measure them in M5)

| Step | Model | Tokens in / out | Cost |
|---|---|---|---|
| Plan (structured output; system prompt + metric enum cached) | Haiku 5.5 | 2,500 / 250 | $0.0004 |
| Router fallback (only when the classifier is unsure) | Haiku 5.5 | 800 / 50 | $0.0001 |
| Composer, Sonnet option | Sonnet 5.5 | 7,000 / 600 | **$0.020** |
| Composer, Haiku option | Haiku 5.5 | 7,000 / 600 | **$0.001** |
| Verifier (about 4 text claims, parallel) | Haiku 5.5 | 4,800 / 320 | $0.0006 |
| **Total per question** | | | **≈ $0.021 (Sonnet composer) / ≈ $0.002 (Haiku composer)** |
| Judge (eval only; FinanceBench + narrative + unanswerable) | Sonnet 5.5 | 2,000 / 200 | $0.006 |

### Project budget against $100 of credits

| Activity | Count | Cost |
|---|---|---|
| Dev iteration on a 100-question dev split (Haiku composer + Sonnet judge ≈ $0.50/run; less with replay) | ~40 runs | ~$15 |
| Composer Pareto: Sonnet vs Haiku on the dev split (≈ $2.40/run with Sonnet) | ~6 runs | ~$15 |
| Full benchmark runs (~600 Qs + judge), Sonnet composer, **Batch API** (≈ $7/run) | ~6 runs (M4 baseline, M5, M6, M7, M12 ×2) | ~$42 |
| Router labels: 4,000 questions labeled by a Sonnet teacher via Batch | 1 | ~$4 |
| Paraphrase generation (Haiku) + drafting the narrative set (Sonnet) | — | ~$2 |
| Live user testing | ~300 questions | ~$6 |
| **Total** | | **~$84** (≈ $16 buffer) |

This is tight [Guessing on token counts]. If the Pareto shows the Haiku composer within the CI of Sonnet, make Haiku the default: full runs then cost about $1–2 each and the budget has plenty of room. If you're running short, cut Sonnet dev comparisons first, never the final test runs.

**Rules that keep this true:**
1. Develop with Haiku 5.5 everywhere. Use Sonnet only for the Pareto comparison and the final numbers.
2. Every full benchmark uses the **Message Batches API** (`make bench MODE=batch`). Batches can take up to 24 h but usually finish much sooner [Likely], so start them before you stop for the day.
3. The LLM client logs tokens and cost for every call to `usage_log` (live) or the results JSON (eval). `make llm-spend` shows the running total.
4. Tests never call the real API. The stub LLM is the default in tests, and a pytest fixture fails any test that opens a socket to `api.anthropic.com`.
5. Set a monthly spend limit in the Claude Console for this workspace.
