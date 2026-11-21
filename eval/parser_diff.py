"""Old vs new section detector over a set of stored filings (read-only measurement).

    uv run python -m eval.parser_diff --items items.jsonl [--old-ref HEAD] [--workers 6]
        [--out docs/milestones/M02-part-state-diff.json]

`items.jsonl` has one JSON object per line: `{"id", "group", "path", "form", "accession", "cik"}`
where `path` is a local `.htm.gz` or `.htm` file. The OLD detector is
`pipelines/parsing/sections.py` as committed at `--old-ref`; the NEW one is the working tree.
For every filing:

* the filing's blocks are parsed once and both detectors run on them; when the chosen sections
  are identical the filing is `identical` (the chunks cannot differ: they are a function of the
  sections);
* otherwise the whole document is parsed both ways (`parse_document`) and the chunks are compared
  by (chunk_id, sha1 of the text): `status_changed` (ok/partial/no_sections differs) or
  `sections_changed`, with the status before and after, the chunk counts, and the chunk ids that
  appear or disappear.

Everything is counted per `group`, with the filings whose OLD status was `ok` called out: those
are the ones a fix must not change.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import types
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from pipelines.parsing import document as doc
from pipelines.parsing import sections as new_sections
from pipelines.parsing.html_blocks import html_to_blocks

_RANK = {"no_sections": 0, "partial": 1, "ok": 2}
_OLD: types.ModuleType | None = None
_OLD_REF = "HEAD"


def load_old(ref: str) -> types.ModuleType:
    src = subprocess.run(
        ["git", "show", f"{ref}:pipelines/parsing/sections.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    mod = types.ModuleType("sections_old")
    sys.modules["sections_old"] = mod
    exec(compile(src, f"sections.py@{ref}", "exec"), mod.__dict__)  # noqa: S102 - our own source
    return mod


def _sig(sections: list[Any]) -> list[tuple[Any, ...]]:
    return [(s.code, s.heading_index, s.start, s.end, s.title, s.body_prefix) for s in sections]


def _status(form: str, sections: list[Any]) -> str:
    if not sections:
        return "no_sections"
    core = doc.CORE_10K if form.startswith("10-K") else doc.CORE_10Q
    return "ok" if all(c in {s.code for s in sections} for c in core) else "partial"


def _meta(item: dict[str, Any]) -> doc.FilingMeta:
    return doc.FilingMeta(
        accession=item["accession"], cik=int(item["cik"]), form=item["form"],
        filed=date(2000, 1, 1), period_of_report=date(2000, 1, 1), primary_doc_url="x",
    )  # fmt: skip


def _parse_with(old: bool, data: bytes, meta: doc.FilingMeta) -> tuple[str, int, dict[str, str]]:
    original = doc.detect_sections
    if old:
        assert _OLD is not None
        doc.detect_sections = _OLD.detect_sections
    try:
        filing, chunks = doc.parse_document(data, meta)
    finally:
        doc.detect_sections = original
    return (
        filing.parse_status,
        filing.n_chunks,
        {c.chunk_id: hashlib.sha1(c.text.encode()).hexdigest() for c in chunks},  # noqa: S324
    )


def compare(item: dict[str, Any]) -> dict[str, Any]:
    global _OLD
    if _OLD is None:
        _OLD = load_old(_OLD_REF)
    raw = Path(item["path"]).read_bytes()
    data = gzip.decompress(raw) if item["path"].endswith(".gz") else raw
    out: dict[str, Any] = {k: item[k] for k in ("id", "group", "form", "accession", "cik")}
    blocks = html_to_blocks(data)
    s_old = _OLD.detect_sections(blocks, item["form"])
    s_new = new_sections.detect_sections(blocks, item["form"])
    # Coverage by block: the characters of blocks that sit inside some section (and so can be
    # chunked) before and after. Unlike chunk hashes it does not move when a boundary re-splits.
    sizes = [len(b.text) for b in blocks]

    def covered(secs: list[Any]) -> set[int]:
        return {i for s in secs for i in range(s.start, s.end)}

    c_old, c_new = covered(s_old), covered(s_new)
    out["doc_chars"] = sum(sizes)
    out["old_cover_chars"] = sum(sizes[i] for i in c_old)
    out["new_cover_chars"] = sum(sizes[i] for i in c_new)
    out["chars_lost"] = sum(sizes[i] for i in c_old - c_new)
    out["chars_gained"] = sum(sizes[i] for i in c_new - c_old)
    # A block that is now a section's heading (or the title line taken from the block after it)
    # is stored in `section_title`, not lost: it leaves the body, not the index. Body loss is what
    # is left (ADR-031 adopts "no filing loses body text" as the gate).
    heading_or_title = {s.heading_index for s in s_new} | {
        s.start - 1 for s in s_new if s.start - 1 > s.heading_index
    }
    lost = c_old - c_new
    out["heading_chars_moved"] = sum(sizes[i] for i in lost if i in heading_or_title)
    out["body_chars_lost"] = sum(sizes[i] for i in lost if i not in heading_or_title)
    out["old_status"] = _status(item["form"], s_old)
    out["new_status"] = _status(item["form"], s_new)
    out["old_codes"] = [s.code for s in s_old]
    out["new_codes"] = [s.code for s in s_new]
    if _sig(s_old) == _sig(s_new):
        out["result"] = "identical"
        return out
    meta = _meta(item)
    st_old, n_old, ch_old = _parse_with(True, data, meta)
    st_new, n_new, ch_new = _parse_with(False, data, meta)
    out.update(old_n_chunks=n_old, new_n_chunks=n_new)
    out["result"] = "status_changed" if st_old != st_new else "sections_changed"
    out["chunks_lost"] = sorted(set(ch_old) - set(ch_new))[:20]
    out["n_chunks_lost"] = len(set(ch_old) - set(ch_new))
    out["n_chunks_gained"] = len(set(ch_new) - set(ch_old))
    out["n_chunks_text_changed"] = sum(
        1 for k in set(ch_old) & set(ch_new) if ch_old[k] != ch_new[k]
    )
    # By text, ignoring the section label: text that was indexed before and is not now.
    t_old, t_new = set(ch_old.values()), set(ch_new.values())
    out["n_texts_lost"] = len(t_old - t_new)
    out["n_texts_gained"] = len(t_new - t_old)
    return out


def _init(ref: str) -> None:
    global _OLD, _OLD_REF
    _OLD_REF = ref
    _OLD = load_old(ref)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_group: dict[str, dict[str, Any]] = {}
    for g in sorted({r["group"] for r in rows}):
        rs = [r for r in rows if r["group"] == g]
        ok_old = [r for r in rs if r["old_status"] == "ok"]
        by_group[g] = {
            "n": len(rs),
            "results": dict(Counter(r["result"] for r in rs)),
            "status_transitions": {
                f"{a}->{b}": n
                for (a, b), n in sorted(
                    Counter((r["old_status"], r["new_status"]) for r in rs).items()
                )
            },
            "filings_with_body_chars_lost": sum(r.get("body_chars_lost", 0) > 0 for r in rs),
            "body_chars_lost": sum(r.get("body_chars_lost", 0) for r in rs),
            "heading_chars_moved": sum(r.get("heading_chars_moved", 0) for r in rs),
            "chars_gained": sum(r.get("chars_gained", 0) for r in rs),
            "filings_losing_any_block_chars": sum(r.get("chars_lost", 0) > 0 for r in rs),
            "status_worse": sum(_RANK[r["new_status"]] < _RANK[r["old_status"]] for r in rs),
            "filings_with_texts_lost": sum(r.get("n_texts_lost", 0) > 0 for r in rs),
            "filings_with_texts_gained": sum(r.get("n_texts_gained", 0) > 0 for r in rs),
            "old_ok": len(ok_old),
            "old_ok_changed": sum(r["result"] != "identical" for r in ok_old),
            "old_ok_not_ok_after": sum(r["new_status"] != "ok" for r in ok_old),
        }
    return {"n": len(rows), "by_group": by_group}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Old vs new section detector over stored filings.")
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--old-ref", default="HEAD")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    items = [json.loads(ln) for ln in args.items.read_text().splitlines() if ln.strip()]
    with ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init, initargs=(args.old_ref,)
    ) as pool:
        rows = list(pool.map(compare, items, chunksize=4))
    # identical filings are only counted (the summary); the rows are the ones that changed
    changed = [r for r in rows if r["result"] != "identical"]
    result = {"old_ref": args.old_ref, "summary": summarize(rows), "rows": changed}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for g, s in result["summary"]["by_group"].items():
        print(g, s["n"], s["results"], "old_ok_changed", s["old_ok_changed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
