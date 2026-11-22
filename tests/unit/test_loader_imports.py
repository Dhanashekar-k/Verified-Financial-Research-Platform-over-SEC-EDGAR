"""The loader runs on the serving box with only its pinned packages: it must import what it uses."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_the_loader_never_reaches_for_pyarrow_submodules_it_did_not_import():
    source = Path("serving/load/load_snapshot.py").read_text(encoding="utf-8")

    # `pa.compute` only exists if something else imported pyarrow.compute first (it did on a
    # laptop, it did not on the box).
    assert "pa.compute" not in source
    assert "pa.csv" not in source
    assert "import pyarrow.compute as pc" in source


def test_a_check_export_embedding_length_works_in_a_fresh_interpreter(tmp_path: Path):
    code = (
        "import pyarrow as pa\n"
        "import serving.load.load_snapshot as m\n"
        "arr = pa.array([[0.0] * 384, [1.0] * 384], pa.list_(pa.float32()))\n"
        "print(m._vectors_as_text(arr)[0].as_py()[:12])\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("[0.0,0.0")
