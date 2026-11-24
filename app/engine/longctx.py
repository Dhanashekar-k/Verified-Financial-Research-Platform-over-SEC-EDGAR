"""Long-context FinanceBench baseline: the whole source document goes into the composer prompt.

No retrieval. This is the comparator for "just stuff the filing in the context window": it shows
what the retrieval baseline and the engine give up (or gain) against a model that sees every
page of the right filing. It is told which document to read (``doc_name`` from the FinanceBench
row), which flatters it relative to a system that must find the filing itself.

Each PDF page becomes one numbered passage with ``chunk_id = fb:<doc_name>#p<page>``, so the
composer prompt, the ``Answer`` schema and the scoring are identical to ``baseline``. A document
whose estimated size exceeds ``MAX_EST_TOKENS`` is not sent: it raises ``DocumentTooLarge``,
which the harness records as a system failure (counted against the system, with the reason).
Token counts are estimated as characters / 4; the billed tokens come from accounting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.engine.answer import Answer
from app.engine.baseline import COMPOSER_SYSTEM, MAX_ANSWER_TOKENS, Passage, build_messages

VERSION = "0.1.0"
MAX_EST_TOKENS = 200_000  # an assumption, not the model's measured window
CHARS_PER_TOKEN = 4  # estimate only
QUESTIONS = Path("eval/sets/financebench.jsonl")
QUESTIONS_DRAFT = Path("eval/sets/drafts/financebench.jsonl")
PDF_DIR = Path("data/raw/financebench/pdfs")


class DocumentTooLarge(RuntimeError):
    """The document's estimated size is over the cap; nothing was sent."""


class LongContextSystem:
    name = "longctx"
    version = VERSION
    retrieves = False  # no retrieval: recall@k is not defined for this system

    def __init__(
        self,
        *,
        questions_path: Path | None = None,
        pdf_dir: Path = PDF_DIR,
        max_est_tokens: int = MAX_EST_TOKENS,
    ) -> None:
        path = questions_path or (QUESTIONS if QUESTIONS.exists() else QUESTIONS_DRAFT)
        self._doc_of: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                self._doc_of[row["question"]] = row["doc_name"]
        self._pdf_dir = pdf_dir
        self._max_est_tokens = max_est_tokens

    def _passages(self, doc_name: str) -> list[Passage]:
        import pymupdf

        path = self._pdf_dir / f"{doc_name}.pdf"
        if not path.exists():
            raise FileNotFoundError(f"no PDF for {doc_name}: {path}")
        passages: list[Passage] = []
        with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
            for number, page in enumerate(pdf, start=1):
                text = page.get_text().strip()
                if text:
                    passages.append(
                        Passage(
                            f"fb:{doc_name}#p{number}", text, None, None, None, "financebench_pdf"
                        )
                    )
        return passages

    def answer(self, question: str, cfg: Any) -> Answer:
        doc_name = self._doc_of.get(question)
        if doc_name is None:
            raise LookupError("question is not in the FinanceBench set (no source document)")
        passages = self._passages(doc_name)
        est = sum(len(p.text) for p in passages) // CHARS_PER_TOKEN
        if est > self._max_est_tokens:
            raise DocumentTooLarge(
                f"{doc_name}: ~{est} tokens (chars/{CHARS_PER_TOKEN}), cap {self._max_est_tokens}"
            )
        answer, _ = cfg.llm.structured(
            cfg.composer,
            COMPOSER_SYSTEM,
            build_messages(question, passages),
            Answer,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        result: Answer = answer
        invented = [c.id for c in result.claims if c.provenance.type == "expr"]
        if invented:
            raise ValueError(f"claims {invented} cite fact ids the system never saw")
        return result
