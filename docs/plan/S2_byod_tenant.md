# S2 (stretch): Bring-your-own-documents tenant (FDE onboarding)

**Time:** about 8 h · **Roles:** FDE (main: this answers "how would you onboard a customer with private documents?"), SDE (isolation, deletion) · **Depends on:** M8, M9, M10 (Lambda parsing image)

## 1. Goal

A tenant uploads its own PDFs/HTML (for example a private company's annual report or an internal memo). They're indexed into a tenant-scoped namespace and searchable together with the public corpus, with **enforced isolation**, **deletion on request** and a per-tenant onboarding eval.

## 2. Design

- `POST /v1/documents` (multipart, ≤ 20 MB, PDF/HTML only, content-type sniffed) → `s3://…/tenants/{tenant_id}/uploads/{doc_id}` (SSE-S3) → SQS `tenant-docs` → Lambda (the same `pipelines/parsing` image) → chunks with `tenant_id`, `doc_source='tenant_upload'` → loader inserts them into the live chunks table.
- **Isolation, defense in depth:**
  1. Retrieval SQL always includes `tenant_id IS NULL OR tenant_id = $tenant`.
  2. **Postgres row-level security** on `chunks_vN`: `USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::int)`; the API sets `SET LOCAL app.tenant_id` per request. The API's DB role isn't the table owner (owners bypass RLS unless it's forced) [Likely; check the RLS docs for `FORCE ROW LEVEL SECURITY`].
  3. Cache keys include the tenant scope (M8).
- **Verification semantics change:** tenant documents have no XBRL, so numbers from them can only be **quote-verified**. The UI badge says "quoted from your document," never "verified against XBRL." State this in the onboarding doc.
- **Deletion:** `DELETE /v1/documents/{doc_id}` → delete S3 objects + chunks + the tenant's cache entries → an audit row. `DELETE /v1/tenants/{id}/data` deletes everything for the tenant.
- **Onboarding eval:** the tenant uploads 5–10 Q&A pairs about its documents → `make tenant-eval TENANT=…` → an onboarding report (accuracy, citations, refusals). This is the FDE artifact you show in interviews.

## 3. Steps

1. API endpoints + validation + S3 upload (2 h).
2. Queue + Lambda reuse + loader path for tenant chunks (2 h).
3. RLS policy + the API role + `SET LOCAL` per request (1.5 h).
4. Deletion endpoints + audit (1 h).
5. **Isolation tests** (1.5 h): a property test creates random tenants and documents and asserts that tenant A's queries never return tenant B's chunks, *even with a deliberately broken retrieval WHERE clause* (this proves RLS works on its own).

## 4. Definition of done

- [ ] Upload → searchable in under 2 minutes for a 50-page PDF (measured).
- [ ] The isolation property test is green, including the "broken WHERE" variant.
- [ ] Deletion verified (S3 + Postgres + cache).
- [ ] One onboarding report generated for a demo tenant (use a public annual report from a non-SEC company as the stand-in).

## 5. Interview prep

- Walk through onboarding a new customer: data intake, isolation, eval, rollout, deletion.
- Why RLS in addition to application filters.
- How verification claims change for customer documents, and how you communicate that to users.
