-- companies_v{v}: the staging copy of the unversioned `companies` table (replaced in the swap).
CREATE TABLE companies_v{v} (
  cik int PRIMARY KEY, name text NOT NULL, ticker text, sic int, fye_mmdd char(4),
  aliases text[] NOT NULL DEFAULT '{{}}'
)
