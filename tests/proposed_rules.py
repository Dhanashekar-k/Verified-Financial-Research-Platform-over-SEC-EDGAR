"""The ADR-022 oracle lives in `pipelines/duckdb/oracle.py` (the benchmark tooling uses it too).

Re-exported here so tests import it from one familiar place. When Shekar applies
`docs/proposals/ADR-022-fiscal_ref.diff`, the oracle equals `derive_fiscal` itself and both files
can be deleted.
"""

from pipelines.duckdb.oracle import ANNUAL_FORMS, reference_with_proposed_rules, stable_key

__all__ = ["ANNUAL_FORMS", "reference_with_proposed_rules", "stable_key"]
