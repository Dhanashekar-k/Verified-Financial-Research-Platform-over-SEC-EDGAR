# M8: API and engineering quality

**Time:** about 12 h. Do the **security subset (§3.1–3.3, about 4 h) before the M9 alpha deploy** and the rest after. · **Roles:** SDE (main), FDE (operability) · **Depends on:** M5–M7

## 1. Goal

A service a team could run: authenticated, rate-limited, spend-capped, cached, traced, tested and load-tested, with throughput and latency numbers you can defend per route.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `engine.answer()` | `app/api/{main,routes,auth,ratelimit,spend_guard,cache,telemetry,errors}.py` |
| Contracts (`05_data_contracts.md` §5) | OpenAPI snapshot test; RFC 9457 errors |
| — | Redis Lua scripts: token bucket, spend reserve/reconcile, single-flight lock |
| — | `usage_log` written for every request; `make latency-report`, `make llm-spend` |
| — | k6 scripts `load/{numeric_path,cached_path,retrieval_stubbed}.js` + results JSON |
| — | T7 (per-route uncached p95 + cached p95) and T8a/b/c recorded |

## 3. Steps

### 3.1 Auth with invite codes (1.5 h)
- `POST /v1/keys/redeem {invite_code}` → creates an API key `eqa_<32 url-safe random bytes>` and returns it **once**. Store `HMAC-SHA256(pepper, key)`, with the pepper in SSM Parameter Store (env locally).
- Invite codes are also stored hashed, with `max_uses` and `expires_at`. Rate-limit redeem attempts per IP (5/hour) to stop brute force.
- `Authorization: Bearer` lookup is cached in Redis for 60 s; revocation deletes the cache entry.

### 3.2 Rate limiting and the spend guard (1.5 h)
- **Token bucket** in Redis via one Lua script (atomic): per key, capacity 5, refill 20/hour (configurable per tenant). On deny: `429` + `Retry-After`.
- **Global daily LLM spend cap** (for example $2/day during M11):
  1. Before the engine runs: atomically **reserve** an estimated cost (p95 cost per route from `usage_log`) against `spend:{UTC date}`. If that would exceed the cap → `503 spend_cap_reached`.
  2. After: **reconcile** by replacing the reservation with the actual cost.
  3. Explain the race condition this prevents (two concurrent requests both passing a check-then-act) and the remaining slack (estimation error).
- **Per-tenant daily question cap** (`tenants.daily_question_cap`).
- Request limits: question ≤ 500 chars, body ≤ 4 KB; errors never include stack traces.

### 3.3 Edge hardening for the alpha (1 h)
- CORS only for the CloudFront domain. Require the `X-Origin-Verify` header (a secret CloudFront adds); reject requests without it except `/v1/health` from localhost.
- `/v1/health` checks Postgres, Redis, ONNX router loaded and corpus_version.

### 3.4 Cache and single-flight (1.5 h)
- Key = `sha256(normalize(question) | scope | corpus_version | engine_version | router_version)`. `normalize` lowercases, collapses whitespace and strips trailing punctuation. Keep it conservative: two different questions mustn't collide.
- Store the full response JSON with a 7-day TTL. A new corpus version invalidates everything implicitly through the key.
- **Single-flight:** concurrent identical uncached requests take a `SET NX PX` lock; followers wait on the result key (poll with backoff, up to 20 s) instead of paying for duplicate LLM calls.
- Report the cache hit rate from `usage_log` during M11.

### 3.5 Observability (2 h)
- OpenTelemetry SDK + auto-instrumentation (FastAPI, psycopg, httpx, redis) → OTLP → Jaeger.
- Custom spans: `route`, `plan`, `compile`, `retrieve.lexical`, `retrieve.dense`, `rerank`, `compose`, `verify.numeric`, `verify.text`, `verify.entail`, with attributes `model`, `input_tokens`, `output_tokens`, `cost_usd`, `cache_hit`, `n_claims`, `n_dropped`.
- JSON logs carry `trace_id` and `request_id`. `usage_log` is written asynchronously (background task) so it doesn't add latency.
- `make latency-report`: SQL over `usage_log` → p50/p95 per route and per stage (from span exports or stored stage timings).

### 3.6 Tests (2 h)
- **Unit:** the Lua token bucket (real Redis via Testcontainers, with time controlled via a parameter), spend reserve/reconcile under concurrency (20 parallel tasks), cache-key normalization collisions.
- **Integration:** API + Postgres + Redis containers + the stub LLM: redeem → ask → cached ask → 429 → spend-cap 503 → error shapes. The OpenAPI snapshot must match the committed schema (contract drift fails CI).
- CI runs unit + integration on every push. GitHub-hosted runners have Docker for Testcontainers [Likely].

### 3.7 Load tests (2.5 h)
- **Run k6 from a separate t4g.small in the same region** (spot, 30 min), not from home, so you measure the server, not your Wi-Fi. Also report server-side latency from traces.
- `numeric_path.js` (**T8a**): numeric questions, stub LLM (no network, fixed small delay = 0), classifier active. Ramping arrival rate 25 → 400 req/s. Thresholds: p99 < 150 ms, errors < 1%. Report the highest rate that meets them.
- `cached_path.js` (**T8b + T7-cached**): repeated questions; target p95 ≤ 100 ms through CloudFront → EC2.
- `retrieval_stubbed.js` (**T8c**): narrative questions, stub LLM and stub reranker. Measure the reranker separately (pairs/second on 2 vCPU).
- **Uncached T7:** 50 dev questions with real LLMs, sequential; p50/p95 per route and a stage breakdown. Name what dominates (expected: the composer) [Likely].
- Tune: gunicorn with 3 uvicorn workers on 2 vCPU (try 2–4), the asyncpg/psycopg pool size, and `hnsw.ef_search`. Record each change and its effect.

## 4. Claude Code session plan

```
/milestone M8
Security subset first (before the M9 alpha): auth + invite redemption (§3.1), token bucket and
spend guard Lua scripts (§3.2), edge hardening (§3.3). Testcontainers tests for each, including
a concurrency test for spend reserve/reconcile. Plan first and explain the Lua atomicity argument.
```
```
Later: cache + single-flight (§3.4), OTel wiring (§3.5), integration tests + the OpenAPI snapshot (§3.6).
```
```
Then: k6 scripts per §3.7 with thresholds, a Terraform snippet or Make target to launch a spot
t4g.small load generator with k6 preinstalled (user_data), and a results parser that writes
load/results/*.json. Don't launch anything. I will.
```

## 5. You write

The concurrency argument for the spend guard (ADR-019), the interpretation of the load results, and the scaling discussion ("what breaks at 100×") in `docs/results.md` prose, around the auto tables.

## 6. Definition of done

- [ ] Redeem/auth/rate-limit/spend-cap/cache all covered by integration tests in CI.
- [ ] T7 (per route uncached, cached) and T8a/b/c measured from a same-region load generator; numbers in results JSON.
- [ ] A Jaeger screenshot of one full trace for the README.
- [ ] `make llm-spend` and `make latency-report` work against `usage_log`.

## 7. Pitfalls

- Measuring throughput with the reranker on CPU and calling it T8 (see `01_review_and_changes.md` §2.6).
- A rate limiter implemented as GET + SET (not atomic).
- Cache keys without `corpus_version` serving stale answers after a reload.
- Logging full questions together with API keys. Never log keys; log `key_id`.

## 8. Interview prep

- Token bucket vs sliding window; why Lua; what happens when Redis is down (fail closed for the public demo).
- The spend guard race and your fix.
- Your latency breakdown and the top optimization by measured impact.
- What breaks first at 100× load and the order you'd fix it in.
