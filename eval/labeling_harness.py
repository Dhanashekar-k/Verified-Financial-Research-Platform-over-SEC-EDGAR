"""A labeling harness for `eval/sets/sections_gold.jsonl` that is independent of the parser.

    python -m eval.labeling_harness prepare   # pick filings, write text files and the template
    python -m eval.labeling_harness fill      # derive first_sentence from the offsets you set
    python -m eval.labeling_harness check     # validate a filled template before promoting it

INDEPENDENCE. The gold must not be derived from what it grades. This module imports nothing from
`pipelines.parsing` (a test fails if it ever does), extracts text with its own minimal routine
(scripts, styles, the iXBRL header and `display:none` removed; one text node per line), and finds
candidates with one regex over those lines. Template rows hold no parser output: the parser's
section assignments are not shown anywhere. Filings are CHOSEN with the earlier measurements (TOC
style, multi-registrant, items the parser missed) so the sample is stratified, but that only picks
the filing; it says nothing about where any section starts. The strata are kept in a side file.

OUTPUTS. For each chosen filing a text file `<out-dir>/<ticker>_<accession>.txt` (scratch,
gitignored), and one template row per required item (1, 1A, 7, 7A, 8):

    {"accession", "section", "first_sentence": "", "labeled_by": "",    # what sections_eval reads
     "text_file", "present": null, "true_start_offset": null,           # you fill these
     "candidates": [{"offset", "line", "text", "context"}],             # neutral regex hits
     "notes": ""}

`true_start_offset` is the character offset (in the text file) of the line where the section really
begins, taken from a candidate or any other offset; `present` is false if the filing has no such
section. `fill` then proposes `first_sentence` (the first sentence of the first body line of 40 or
more characters after that line) for you to confirm or retype. Rows with `present: false` must be
dropped before promotion. Writing under `eval/sets/` is yours: a hook blocks edits there and
`make gold-checksums` is run by you after you copy the final file.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

from selectolax.lexbor import LexborHTMLParser

REQUIRED = ("1", "1A", "7", "7A", "8")
DEFAULT_CACHE = Path("data/fixture_candidates/blast")
DEFAULT_BLAST = Path("docs/milestones/M02-blast-radius.json")
DEFAULT_OUT = Path("data/fixture_candidates/labeling")
DEFAULT_TEMPLATE = Path("eval/sets/drafts/sections_gold_template.jsonl")
MIN_BODY_LINE = 40
CONTEXT_LINES = 3
_ITEM = re.compile(
    r"^(?:part\s+(?:iv|iii|ii|i)\b[\s,.:;|\-–—]*)?item\s*(\d{1,2})\s*([a-c])?(?![a-z0-9])",
    re.IGNORECASE,
)
_HIDDEN = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_OPAQUE = re.compile(r"^[a-z]?[0-9a-f]{16,}(_\d+)?$", re.IGNORECASE)
_ITEM_ID = re.compile(r"item[_\- ]?\d", re.IGNORECASE)
_ITEM_LINK = re.compile(r"^\s*(part\s+[ivx]+[\s,.\-–—:]*)?item\s*\d", re.IGNORECASE)


# -- text -------------------------------------------------------------------------------


def plain_text(html: bytes | str) -> str:
    """The document as lines of visible text, one text node per line, blank lines dropped."""
    raw = html if isinstance(html, str) else html.decode("utf-8", "replace")
    tree = LexborHTMLParser(raw)
    for node in tree.css("script,style,head,noscript,ix\\:header,[style]"):
        style = node.attributes.get("style") or ""
        if (
            node.tag != "-text"
            and (node.tag != "html")
            and (
                node.tag in ("script", "style", "head", "noscript", "ix:header")
                or _HIDDEN.search(style)
            )
        ):
            node.decompose()
    body = tree.body
    text = body.text(separator="\n", strip=False) if body is not None else ""
    lines = (" ".join(line.replace("\xa0", " ").split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def find_hits(text: str) -> list[dict[str, Any]]:
    """Every line that starts with `[Part X] Item N[A-C]`, with its offset and a few lines after."""
    lines = text.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    hits = []
    for i, line in enumerate(lines):
        m = _ITEM.match(line)
        if m:
            code = m.group(1) + (m.group(2) or "").upper()
            hits.append(
                {
                    "code": code,
                    "offset": offsets[i],
                    "line": i + 1,
                    "text": line[:160],
                    "context": " / ".join(lines[i + 1 : i + 1 + CONTEXT_LINES])[:240],
                }
            )
    return hits


def toc_profile(raw: bytes) -> dict[str, Any]:
    """Anchor-linked TOC? and what its target ids look like (opaque / named / none)."""
    tree = LexborHTMLParser(raw.decode("utf-8", "replace"))
    ids = {n.attributes.get("id") for n in tree.css("[id]")}
    ids |= {n.attributes.get("name") for n in tree.css("a[name]")}
    links = [
        a
        for a in tree.css("a[href^='#']")
        if len(a.attributes.get("href") or "") > 1 and a.attributes["href"][1:] in ids
    ]
    toc = [
        a
        for a in links
        if _ITEM_LINK.match(a.text(strip=True) or "") or _ITEM_ID.search(a.attributes["href"])
    ]
    style = "none"
    if len(toc) >= 5:
        opaque = sum(bool(_OPAQUE.match(a.attributes["href"][1:])) for a in toc)
        style = "opaque" if opaque > len(toc) / 2 else "named"
    return {"internal_links": len(links), "item_toc_links": len(toc), "style": style}


# -- selection --------------------------------------------------------------------------


def choose(rows: list[dict[str, Any]], per_stratum: dict[str, int]) -> list[dict[str, Any]]:
    """Stratified, deterministic: the smallest filings first (easier to label), one CIK once.

    Each row needs: accession, cik, bytes, toc_style, multi_registrant, miss_causes (dict).
    Strata: opaque / named / none (TOC style, filings the earlier measurement found complete),
    multi_registrant, and missing_item (one per distinct cause where possible).
    """
    used: set[int] = set()
    picked: list[dict[str, Any]] = []

    def take(stratum: str, pool: list[dict[str, Any]], n: int) -> None:
        for row in sorted(pool, key=lambda r: (r["bytes"], r["accession"])):
            if n == 0:
                return
            if row["cik"] in used:
                continue
            used.add(row["cik"])
            picked.append({**row, "stratum": stratum})
            n -= 1

    complete = [r for r in rows if not r["miss_causes"]]
    # Filings that miss only SOME items: one that misses all five (a pure cross-reference index
    # with page ranges) has no well-defined section starts to label.
    partial = [r for r in rows if r["miss_causes"] and len(r["miss_causes"]) < len(REQUIRED)]
    for cause in ("toc_or_index_only", "no_mention"):
        pool = [r for r in partial if cause in r["miss_causes"].values()]
        take(f"missing_item:{cause}", pool, 1)
    take(
        "multi_registrant",
        [r for r in complete if r["multi_registrant"]],
        per_stratum.get("multi_registrant", 2),
    )
    for style in ("none", "named", "opaque"):
        take(
            f"toc_{style}",
            [r for r in complete if r["toc_style"] == style and not r["multi_registrant"]],
            per_stratum.get(style, 2),
        )
    return picked


# -- template ---------------------------------------------------------------------------


def template_rows(
    accession: str, text_file: str, hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for item in REQUIRED:
        rows.append(
            {
                "accession": accession,
                "section": item,
                "first_sentence": "",
                "labeled_by": "",
                "text_file": text_file,
                "present": None,
                "true_start_offset": None,
                "candidates": [
                    {k: h[k] for k in ("offset", "line", "text", "context")}
                    for h in hits
                    if h["code"] == item
                ],
                "notes": "",
            }
        )
    return rows


def first_sentence(text: str, offset: int) -> str | None:
    """First sentence of the first line with MIN_BODY_LINE+ characters after the heading line."""
    lines = text[offset:].split("\n")[1:]
    for line in lines:
        if len(line) >= MIN_BODY_LINE:
            m = re.match(r"(.+?[.!?])(?:\s|$)", line)
            return (m.group(1) if m else line).strip()
    return None


def fill_rows(rows: list[dict[str, Any]], texts: dict[str, str]) -> int:
    """Set first_sentence from true_start_offset where it is set and first_sentence is blank."""
    filled = 0
    for row in rows:
        if (
            row.get("present")
            and row.get("true_start_offset") is not None
            and not row["first_sentence"]
        ):
            sentence = first_sentence(texts[row["text_file"]], row["true_start_offset"])
            if sentence:
                row["first_sentence"] = sentence
                filled += 1
    return filled


def check_rows(rows: list[dict[str, Any]], texts: dict[str, str]) -> list[str]:
    """Problems that would make a row wrong or unusable as gold."""
    problems = []
    for n, row in enumerate(rows, start=1):
        where = f"row {n} ({row.get('accession')} {row.get('section')})"
        if row.get("present") is None:
            problems.append(f"{where}: present is not set")
            continue
        if row["present"] is False:
            problems.append(f"{where}: present is false; drop this row before promoting")
            continue
        text = texts.get(row.get("text_file", ""))
        offset = row.get("true_start_offset")
        if text is None or offset is None or not 0 <= offset < len(text):
            problems.append(f"{where}: true_start_offset is missing or outside the text")
            continue
        if not row.get("first_sentence", "").strip():
            problems.append(f"{where}: first_sentence is blank")
        elif _squash(row["first_sentence"])[:40] not in _squash(text[offset : offset + 3000]):
            problems.append(
                f"{where}: first_sentence does not appear within 3000 characters after the offset"
            )
        if not row.get("labeled_by", "").strip():
            problems.append(f"{where}: labeled_by is blank")
    return problems


def _squash(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def load_texts(rows: list[dict[str, Any]], out_dir: Path) -> dict[str, str]:
    return {f: (out_dir / f).read_text(encoding="utf-8") for f in {r["text_file"] for r in rows}}


# -- commands ---------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> int:
    blast = json.loads(args.blast.read_text())
    rows = []
    for f in blast["filings"]:
        path = args.cache / f"{f['ticker']}_{f['accession']}.htm.gz"
        if not path.is_file() or f["sgml_wrapper"]:
            continue
        rows.append({**f, "toc_style": toc_profile(gzip.decompress(path.read_bytes()))["style"]})
    chosen = choose(
        rows,
        {
            "multi_registrant": args.multi,
            "none": args.none,
            "named": args.named,
            "opaque": args.opaque,
        },
    )
    template: list[dict[str, Any]] = []
    strata: list[dict[str, Any]] = []
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for f in chosen:
        raw = gzip.decompress((args.cache / f"{f['ticker']}_{f['accession']}.htm.gz").read_bytes())
        text = plain_text(raw)
        name = f"{f['ticker']}_{f['accession']}.txt"
        (args.out_dir / name).write_text(text, encoding="utf-8")
        hits = find_hits(text)
        template.extend(template_rows(f["accession"], name, hits))
        per_item = {i: sum(h["code"] == i for h in hits) for i in REQUIRED}
        strata.append(
            {
                "accession": f["accession"], "ticker": f["ticker"],
                "report_year": f["report_year"], "url": f["url"], "stratum": f["stratum"],
                "bytes": f["bytes"], "text_chars": len(text), "hits_per_required_item": per_item,
            }
        )  # fmt: skip
        print(
            f"{f['stratum']:34} {f['ticker']:7} FY{f['report_year']} {f['accession']} "
            f"{len(text):>10,} chars  hits={per_item}"
        )
    write_jsonl(args.template, template)
    args.template.with_suffix(".strata.json").write_text(
        json.dumps(strata, indent=1) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {len(template)} rows for {len(chosen)} filings to {args.template}")
    print(f"text files are in {args.out_dir}")
    return 0


def fill(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.template)
    n = fill_rows(rows, load_texts(rows, args.out_dir))
    write_jsonl(args.template, rows)
    print(f"proposed first_sentence for {n} rows; confirm or retype each one")
    return 0


def check(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.template)
    problems = check_rows(rows, load_texts(rows, args.out_dir))
    for p in problems:
        print(p)
    print(
        f"{len(rows) - len(problems)} of {len(rows)} rows look ready"
        if problems
        else f"all {len(rows)} rows look ready"
    )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parser-independent labeling harness for the gold set."
    )
    parser.add_argument("command", choices=["prepare", "fill", "check"])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--blast", type=Path, default=DEFAULT_BLAST)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--multi", type=int, default=2)
    parser.add_argument("--none", type=int, default=2)
    parser.add_argument("--named", type=int, default=2)
    parser.add_argument("--opaque", type=int, default=2)
    args = parser.parse_args(argv)
    return {"prepare": prepare, "fill": fill, "check": check}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
