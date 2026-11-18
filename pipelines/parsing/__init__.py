"""Shared filing-text parsing (M02 section 3.3).

Pure Python: no Spark, no pandas. Reused by the Spark job (`pipelines/spark/parse_filings.py`) and
by the M10 Lambda, so it must stay importable on the EMR image's Python and on Lambda.
"""
