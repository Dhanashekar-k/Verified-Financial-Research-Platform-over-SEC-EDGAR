"""`pipelines.spark.fiscal` must not depend on a `.pth` file to get `distutils`.

PySpark 3.5's `pandas_udf` (used by `applyInPandas`) imports `distutils`, which Python 3.12
removed; `setuptools` supplies a shim, normally through `distutils-precedence.pth`. On macOS a
`hidden` flag on that file makes `site` skip it (it happened repeatedly on the development
machine), and every Spark test then failed with `No module named 'distutils'`. The module
imports `setuptools` itself when `distutils` is missing, so the shim does not depend on `site`.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def run_without_site(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a Python started with -S: no site-packages and no `.pth` processing, so
    the paths are added by hand (the situation of a skipped `distutils-precedence.pth`)."""
    env = {"PYTHONPATH": f"{REPO}:{sysconfig.get_paths()['purelib']}", "PATH": ""}
    return subprocess.run(
        [sys.executable, "-S", "-c", code], capture_output=True, text=True, env=env, timeout=120
    )


def test_distutils_is_missing_without_the_pth_hook():
    """The premise: with no `.pth` processing a bare interpreter has no `distutils`."""
    result = run_without_site("import distutils")

    assert result.returncode != 0
    assert "No module named 'distutils'" in result.stderr


def test_importing_the_fiscal_module_makes_distutils_importable_without_the_pth_hook():
    result = run_without_site(
        "import pipelines.spark.fiscal\nimport distutils.version\nprint('distutils ok')"
    )

    assert result.returncode == 0, result.stderr
    assert "distutils ok" in result.stdout
