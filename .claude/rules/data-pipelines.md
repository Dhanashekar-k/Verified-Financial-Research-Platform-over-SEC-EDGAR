---
paths:
  - "pipelines/**"
  - "serving/**"
  - "infra/**"
---

# Data pipelines, serving loads and infrastructure

- All SEC HTTP goes through `pipelines/fetch/sec_client.py` (User-Agent from `SEC_USER_AGENT`, ≤ 8 req/s globally, exponential backoff with jitter, honor `Retry-After`).
- Every job is **idempotent**: rerunning a quarter/day/version produces the same final state. Use Iceberg `overwritePartitions()` or `MERGE`, never blind appends. Loaders drop and recreate `*_vN` tables.
- Spark: explicit schemas (never infer), the `spark_session(env)` factory, lowercase snake_case names, `xxhash64` for keys (no Python UDF dependency for hashing). Avoid Python UDFs on the hot path; use `mapInPandas` when Python is unavoidable (parsing, embedding).
- `pipelines/parsing` must stay free of Spark imports (it's reused by Lambdas) and compatible with the EMR Python version.
- Per-document failures are captured (`parse_status`, `parse_error`), never fatal to the job.
- Tests use small local fixtures, moto for AWS, and respx for HTTP. No real AWS or SEC calls in tests.
- Terraform: every resource gets `default_tags`; least-privilege IAM with a comment explaining each permission; no NAT gateway, no ALB, no public SSH; log groups get 7-day retention; the EMR Serverless application has auto-stop and a maximum capacity. Run `fmt` and `validate` only. Shekar runs `plan` and `apply`.
