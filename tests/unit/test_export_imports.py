"""The Glue export job must start without the model runtime (fastembed) installed."""

from __future__ import annotations

import subprocess
import sys

BLOCK = (
    "import sys\n"
    "sys.modules['fastembed'] = None\n"
    "sys.modules['pipelines.common.embedding'] = None\n"
    "import pipelines.spark.export_serving as m\n"
    "from pipelines.common.manifest import Manifest\n"
    "assert m.MODEL_NAME == 'BAAI/bge-small-en-v1.5' and m.DIM == 384\n"
    "print('ok')\n"
)


def test_export_serving_and_the_manifest_import_without_fastembed():
    done = subprocess.run([sys.executable, "-c", BLOCK], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("ok")


def test_the_pinned_constants_are_the_same_objects_everywhere():
    from pipelines.common import embedding, embedding_artifact

    for name in (
        "MODEL_NAME",
        "DIM",
        "QUERY_INSTRUCTION",
        "ARTIFACT_HF_REPO",
        "ARTIFACT_HF_REVISION",
        "ARTIFACT_ONNX_FILE",
        "ARTIFACT_ONNX_SHA256",
    ):
        assert getattr(embedding, name) == getattr(embedding_artifact, name)
