"""The pinned embedding model, as plain constants (no third-party imports).

Kept apart from `embedding.py`, which imports fastembed and numpy: the Glue export job and the
serving loader need these names (they go into the manifest) but not the model runtime.
"""

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
ARTIFACT_HF_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
ARTIFACT_ONNX_FILE = "model_optimized.onnx"
# Commit of ARTIFACT_HF_REPO the model was fetched from (from the Hugging Face download metadata,
# 2026-11-21); `scripts/stage_model.py download` fetches exactly this revision.
ARTIFACT_HF_REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
# sha256 of the ONNX file fetched from ARTIFACT_HF_REPO on 2026-11-21 (`make fetch-model`,
# 66,465,124 bytes). Recorded in the export manifest as `embedding_artifact.onnx_sha256`; callers
# that must not drift pass it as `expected_onnx_sha256`.
ARTIFACT_ONNX_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
