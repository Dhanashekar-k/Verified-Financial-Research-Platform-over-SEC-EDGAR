"""Build (and optionally upload) the code bundle EMR Serverless needs.

    uv run python -m pipelines.cloud.package [--out dist] [--upload s3://<lake>/artifacts]

Writes `pipelines-<git sha>.zip` (every `pipelines/**/*.py` plus the bundled tokenizer, no tests
and no caches; reproducible: sorted entries, fixed timestamps) and copies `entrypoint.py` next to
it. With `--upload` both go to `s3://<bucket>/artifacts/<sha>/`; that is a write to the cloud, so
Claude only builds locally and the owner uploads (CLAUDE.md rule 8).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

from pipelines.common.object_store import open_store

FIXED_TIME = (2026, 1, 1, 0, 0, 0)
INCLUDE_SUFFIXES = {".py", ".json"}
SKIP_PARTS = {"__pycache__", "tests"}


def git_sha(root: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(f"cannot read the git sha: {out.stderr.strip()}")
    return out.stdout.strip()


def is_dirty(root: Path) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return out.returncode != 0 or bool(out.stdout.strip())


def files_to_bundle(root: Path) -> list[Path]:
    package = root / "pipelines"
    return sorted(
        p
        for p in package.rglob("*")
        if p.is_file() and p.suffix in INCLUDE_SUFFIXES and not (SKIP_PARTS & set(p.parts))
    )


def build_zip(root: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files_to_bundle(root):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return out


def upload(location: str, sha: str, zip_path: Path, entrypoint: Path) -> list[str]:
    store = open_store(f"{location.rstrip('/')}/{sha}")
    uris = []
    for path, key in ((zip_path, "pipelines.zip"), (entrypoint, "entrypoint.py")):
        store.put(key, path.read_bytes())
        uris.append(store.uri(key))
    return uris


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the EMR code bundle.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--upload", default=None, help="s3://bucket/artifacts (a cloud write)")
    parser.add_argument("--allow-dirty", action="store_true", help="upload from a dirty tree")
    args = parser.parse_args(argv)

    sha = git_sha(args.root)
    zip_path = build_zip(args.root, args.out / f"pipelines-{sha}.zip")
    entry_src = args.root / "pipelines" / "cloud" / "entrypoint.py"
    n_files = len(files_to_bundle(args.root))
    print(f"built {zip_path} ({zip_path.stat().st_size} bytes, {n_files} files)")
    if args.upload:
        if not args.upload.startswith("s3://"):
            print("error: --upload must be an s3:// location", file=sys.stderr)
            return 2
        if is_dirty(args.root) and not args.allow_dirty:
            print(
                f"error: uncommitted changes: sha {sha} would name a bundle that is not that "
                "commit; commit first (or pass --allow-dirty)",
                file=sys.stderr,
            )
            return 2
        for uri in upload(args.upload, sha, zip_path, entry_src):
            print(f"uploaded {uri}")
    else:
        print("not uploaded (pass --upload s3://<bucket>/artifacts to publish it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
