"""EMR Serverless entry point: `entrypoint.py <module> [args...]` runs `python -m <module> args`.

`spark-submit` needs a script file, but the jobs are modules (`pipelines.spark.parse_filings`),
which import each other through the package shipped with `--py-files`.
"""

from __future__ import annotations

import runpy
import sys


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("usage: entrypoint.py <module> [args...]")
    module = argv[1]
    sys.argv = [module, *argv[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv)
