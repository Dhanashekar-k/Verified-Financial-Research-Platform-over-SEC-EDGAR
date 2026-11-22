"""Which chunks are in the embedding and serving scope (ADR-037, contract section 2.2).

One definition, used by the embedding job (`--scope served`) and the serving export, so the set
that is embedded and the set that is exported cannot drift apart.
"""

from __future__ import annotations

from pyspark.sql import Column
from pyspark.sql import functions as F

SERVED_FORMS = ("10-K", "10-K/A")
SERVED_DOC_SOURCES = ("financebench_pdf",)
NOT_EMBEDDED_FORMS = ("10-Q", "10-Q/A")
NOT_EMBEDDED_DOC_SOURCE = "edgar_html"

SERVED_RULE = "form IN ('10-K','10-K/A') OR doc_source = 'financebench_pdf'"
NOT_EMBEDDED_RULE = "doc_source = 'edgar_html' AND form IN ('10-Q','10-Q/A')"


def served_predicate() -> Column:
    """True for chunks that are embedded and served.

    NULL in one of the two columns does not stop the other from qualifying; both NULL is not served.
    """
    in_scope = F.col("form").isin(list(SERVED_FORMS)) | F.col("doc_source").isin(
        list(SERVED_DOC_SOURCES)
    )
    return F.coalesce(in_scope, F.lit(False))


def not_embedded_predicate() -> Column:
    """True for the chunks deliberately left out of the embeddings (EDGAR 10-Q and 10-Q/A)."""
    out = (F.col("doc_source") == NOT_EMBEDDED_DOC_SOURCE) & F.col("form").isin(
        list(NOT_EMBEDDED_FORMS)
    )
    return F.coalesce(out, F.lit(False))
