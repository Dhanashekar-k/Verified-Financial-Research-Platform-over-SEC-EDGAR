"""Glue entry-point plumbing: turn Glue's job arguments into the shared job's own CLI.

Glue starts a script with EVERY job argument in `sys.argv` (its own `--JOB_NAME`, `--TempDir`,
`--enable-metrics`, ... plus ours), so forwarding `sys.argv` to a strict `argparse` parser would
fail. Each Glue script names the options its job understands; everything else is ignored.

The jobs themselves (`pipelines.spark.fsds_to_iceberg`, `pipelines.spark.normalize_facts`) are the
same code the laptop and EMR run; this module adds nothing but `--env glue` and the lake bucket.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping, Sequence


def forwarded_args(
    options: Sequence[str],
    argv: Sequence[str],
    defaults: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """`(lake_bucket, args for the job's main())` from Glue's `argv`.

    `options` are the job's option names without `--` (e.g. `quarters`, `fiscal-years`).
    `defaults` may use `{bucket}`. An option that is absent or empty is not forwarded, so the
    job's own default applies.
    """
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--lake-bucket", required=True)
    for opt in options:
        parser.add_argument(f"--{opt}", default=None)
    known, _glue_own = parser.parse_known_args(list(argv))

    args = ["--env", "glue"]
    for opt in options:
        value = getattr(known, opt.replace("-", "_"))
        if value in (None, "") and defaults and opt in defaults:
            value = defaults[opt].format(bucket=known.lake_bucket)
        if value not in (None, ""):
            args += [f"--{opt}", str(value)]
    return known.lake_bucket, args


def run_glue_job(
    main: Callable[[list[str]], int],
    options: Sequence[str],
    argv: Sequence[str],
    defaults: Mapping[str, str] | None = None,
) -> None:
    """Run `main` the way Glue needs it: a non-zero return fails the job."""
    bucket, args = forwarded_args(options, argv, defaults)
    os.environ["EDGARQA_LAKE_BUCKET"] = bucket
    code = main(args)
    if code != 0:
        raise RuntimeError(f"{getattr(main, '__module__', 'job')} exited with code {code}")
