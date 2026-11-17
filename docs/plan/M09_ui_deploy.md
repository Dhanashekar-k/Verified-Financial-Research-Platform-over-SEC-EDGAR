# M9: UI and cloud deployment (alpha by about Dec 10)

**Time:** about 12 h · **Roles:** FDE (a link people can use), SDE (IaC, CI/CD), cloud · **Depends on:** M6, plus the M8 security subset

## 1. Goal

A link a recruiter or tester can open on a phone: a static UI on S3 + CloudFront, the API + Postgres + Redis on one Graviton instance behind CloudFront, everything in Terraform, deployed from GitHub Actions with OIDC, and protected by invite codes and a spend cap.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| API image, engine, ONNX router, exports | Terraform modules `serving`, `cdn`, `ecr`, permissions for `github_oidc` |
| Next.js app | `https://<id>.cloudfront.net` (optionally a custom domain later) |
| — | `.github/workflows/deploy.yml` (tag → build arm64 → ECR → SSM deploy → UI sync → invalidation) |
| — | `docs/runbook.md` (deploy, rollback, corpus rollback, key rotation, backups, teardown) |
| — | Alpha checklist complete; invite codes issued (one per tester) |

## 3. Steps

### 3.1 UI (4 h; Claude builds it, you set the UX priorities)
Next.js App Router with `output: 'export'`, TypeScript and Tailwind:
- **Ask page:** question box with examples (one numeric, one narrative, one hybrid, one that should refuse), a submit button, and a progress state ("routing → retrieving → verifying").
- **Answer view:** rendered `answer_markdown` with `[n]` markers; numbers with a **"verified against XBRL"** badge (tooltip: tag, period, accession); a `partially verified` or `refused` banner when relevant; cost and latency in a collapsible "details" section (FDE-style transparency).
- **Citation drawer:** chunk text with the quote highlighted, section and filing info, and an "Open on SEC.gov" link (anchor or text fragment). Fact citations show the tag, value, period and filing link.
- **Refusal panel:** "Here's what I found / here's what's missing."
- **Feedback:** 👍/👎 plus an optional comment → `POST /v1/feedback`.
- **Invite redemption:** first visit asks for an invite code → stores the API key in `localStorage` (wrapped in try/catch; fall back to memory).
- **About page:** how it works (architecture image), limitations, "not investment advice," data sources and the FinanceBench attribution (CC-BY-NC).
- Mobile check: phone-width layout; the citation drawer becomes full-screen.

### 3.2 Terraform: serving box (3 h)
`modules/serving`:
- EC2 **t4g.medium**, Amazon Linux 2023 arm64 (AMI from the SSM public parameter), **IMDSv2 required**, gp3 30 GB encrypted, in a public subnet of a minimal VPC (or the default VPC) with **no NAT**.
- Security group: inbound **80 only from the CloudFront origin-facing managed prefix list** (`com.amazonaws.global.cloudfront.origin-facing`) [Likely]; **no port 22**. Admin access is through **SSM Session Manager** (with port-forwarding for Jaeger and Postgres when needed).
- Instance profile: ECR pull, SSM managed instance core, read on `exports/` and `artifacts/` in the lake bucket, read on `/edgarqa/*` SSM parameters, write on `backups/`.
- `user_data`: install Docker + the compose plugin, write `/opt/edgarqa/{docker-compose.yml, deploy.sh}`, create a systemd unit, pull images, start. Compose services: `api` (gunicorn + uvicorn workers, port 8000 → host 80), `postgres` (tuned conf from M3), `redis`, `jaeger` (bound to 127.0.0.1).
- **Backups:** Postgres data is rebuildable from exports **except** `tenants`, `api_keys`, `invite_codes`, `usage_log` and `feedback`. A nightly systemd timer `pg_dump`s those tables to `s3://…/backups/` (7-day lifecycle).

### 3.3 Terraform: CDN (1.5 h)
`modules/cdn`: one CloudFront distribution:
- Default behavior → the S3 UI bucket via **Origin Access Control** (the bucket stays private).
- `/api/*` behavior → the EC2 origin (its public DNS name, HTTP port 80), **CachingDisabled**, an origin-request policy forwarding `Authorization`, `Content-Type` and the query string, plus the custom origin header `X-Origin-Verify: <secret from SSM>`.
- Optional: a custom domain + ACM certificate in us-east-1 later.

### 3.4 Deploy pipeline (2 h)
`.github/workflows/deploy.yml` (on tag `v*`):
1. Assume the IAM role via **OIDC**.
2. Build **arm64** images (prefer native arm64 runners if your repo has them; otherwise buildx + QEMU, which is slower) → push to ECR with the tag = git SHA.
3. `aws ssm send-command` → `/opt/edgarqa/deploy.sh <sha>`: pull, `docker compose up -d`, poll `/v1/health` (60 s), and **roll back to the previous tag** if it fails.
4. Web: `next build` → `aws s3 sync out/ s3://<ui-bucket> --delete` → CloudFront invalidation `/*`.
- Data: `make remote-load VERSION=N` runs the loader container on the box via SSM (M3 loader with swap).

### 3.5 Alpha checklist (1.5 h)
- [ ] Spend cap set ($2/day) and tested in production (temporarily set it to $0.01 and confirm the 503).
- [ ] Console spend limit for the Claude workspace set.
- [ ] One invite code per tester (named in `tenants`, so feedback is attributable).
- [ ] Error page friendly; no stack traces.
- [ ] Privacy note on the About page: questions are logged to improve the system; don't enter personal data.
- [ ] Phone test on iOS Safari and Android Chrome.
- [ ] Re-measure the M3 latencies on EC2 (numeric lookup, hybrid search) and record them.
- [ ] `make stop-serving` and `make destroy-compute` tested once.

## 4. Claude Code session plan

```
/milestone M9
Step 1: Next.js app per M09 §3.1 against the API contract in docs/plan/05_data_contracts.md §5.
Static export. Mock API mode for local dev using recorded responses from eval/cassettes.
Show me screenshots-equivalent descriptions of each state before polishing.
```
```
Step 2: Terraform modules serving + cdn + ecr + github_oidc permissions per §3.2–3.3. fmt/validate only.
Explain every IAM permission you grant and why it's needed.
```
```
Step 3: deploy.yml + /opt/edgarqa/deploy.sh with health-check rollback + make remote-load per §3.4.
Then docs/runbook.md: deploy, rollback (app + corpus), rotate pepper/keys, restore backups, teardown.
```
You run `terraform plan`, read the IAM and security-group diffs line by line, apply, and do the alpha checklist.

## 5. You write

The ADR for "one EC2 box vs Lambda + Aurora" (ADR-005) with your measured numbers, the review of the IAM policies, and the runbook's incident section.

## 6. Definition of done

- [ ] The public URL works on a phone; invite redemption → ask → citation → feedback round trip.
- [ ] The tag → deploy pipeline works, including an intentional bad deploy that rolls back.
- [ ] Security group admits only CloudFront; no SSH; IMDSv2 enforced (show the Terraform).
- [ ] Alpha checklist complete; testers invited with codes.
- [ ] `make destroy-compute` removes the compute and keeps the lake, budgets and IAM.

## 7. Pitfalls

- Forgetting to forward `Authorization` to the origin (every request becomes 401).
- An x86 image on a Graviton box (`exec format error`). Build arm64.
- Leaving an Elastic IP allocated after destroy (charged).
- A CloudFront origin using the EC2 public DNS, which changes on stop/start. Fine for an always-on alpha; for "demo on demand" the wake Lambda must update the origin (`04_aws_and_cost_plan.md` §3).

## 8. Interview prep

- Walk through the request path and every security control on it.
- Why CloudFront in front of a single EC2 (TLS, caching for the UI, origin lockdown, no ALB cost).
- How deploys roll back, and how a corpus rolls back (two different mechanisms).
- What you'd change for production at a company (multi-AZ RDS, ALB + ASG or ECS, WAF, secrets rotation).
