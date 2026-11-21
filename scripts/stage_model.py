"""Fetch the quantized bge model for local use and stage it in S3 for the Glue embedding job.

    python -m scripts.stage_model download        # Hugging Face -> data/models/bge-small-en-v1.5
    python -m scripts.stage_model upload --to s3://<lake>/artifacts/models/bge-small-en-v1.5
    python -m scripts.stage_model upload --to s3://<lake>/artifacts/models/bge-small-en-v1.5 --yes

The first `upload` is a dry run; `--yes` writes to S3 (a cloud write). ADR-032.

`download` is a read from Hugging Face into a new local directory (gitignored `data/`). It prints
the sha256 of every file; the one of `model_optimized.onnx` is pinned as `ARTIFACT_ONNX_SHA256` in
`pipelines/common/embedding.py` and recorded in the export manifest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipelines.common.embedding import (
    ARTIFACT_HF_REPO,
    ARTIFACT_HF_REVISION,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
)
from pipelines.common.model_store import describe_dir, upload_model_dir, write_manifest

DEFAULT_DIR = Path("data/models/bge-small-en-v1.5")


def download(dest: Path) -> int:
    from huggingface_hub import snapshot_download

    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=ARTIFACT_HF_REPO, revision=ARTIFACT_HF_REVISION, local_dir=dest)
    files = write_manifest(dest)
    got = files[ARTIFACT_ONNX_FILE]["sha256"]
    if got != ARTIFACT_ONNX_SHA256:
        print(
            f"{ARTIFACT_ONNX_FILE} sha256 {got} != pinned {ARTIFACT_ONNX_SHA256}; refusing",
            file=sys.stderr,
        )
        return 3
    for name, meta in files.items():
        print(f"{meta['sha256']}  {meta['bytes']:>10}  {name}")
    print(
        f"\nmodel dir: {dest}\n{ARTIFACT_ONNX_FILE} sha256: {files[ARTIFACT_ONNX_FILE]['sha256']}"
    )
    return 0


def upload(src: Path, to: str, yes: bool) -> int:
    files = describe_dir(src)
    if ARTIFACT_ONNX_FILE not in files:
        print(f"{src} has no {ARTIFACT_ONNX_FILE}; run `download` first", file=sys.stderr)
        return 2
    total = sum(int(m["bytes"]) for m in files.values())
    print(f"{len(files)} files, {total} bytes: {src} -> {to}")
    if not yes:
        print("dry run; add --yes to upload (a cloud write)")
        return 0
    keys = upload_model_dir(src, to)
    print(f"uploaded {len(keys)} objects (incl. MODEL_FILES.json)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--dest", type=Path, default=DEFAULT_DIR)
    u = sub.add_parser("upload")
    u.add_argument("--src", type=Path, default=DEFAULT_DIR)
    u.add_argument("--to", required=True)
    u.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "download":
        return download(args.dest)
    return upload(args.src, args.to, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
