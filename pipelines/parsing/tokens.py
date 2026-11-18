"""The embedder's tokenizer (`BAAI/bge-small-en-v1.5`, MIT) used to measure chunk size.

The tokenizer file is bundled in the repo and pinned by SHA-256, so chunk sizes are identical on a
laptop, on EMR and in a Lambda, with no network call. Counts exclude [CLS]/[SEP] (the embedder
adds two, and chunks are capped at 400 of its 512 positions). Truncation and padding are off:
a count must never be silently clipped.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer as HfTokenizer

DEFAULT_PATH = Path(__file__).parent / "assets" / "bge-small-en-v1.5.tokenizer.json"
TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"


class Tokenizer:
    def __init__(self, path: Path = DEFAULT_PATH, expected_sha256: str = TOKENIZER_SHA256) -> None:
        raw = path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        if self.sha256 != expected_sha256:
            raise ValueError(f"tokenizer sha256 {self.sha256} != pinned {expected_sha256}")
        tok = HfTokenizer.from_str(raw.decode("utf-8"))
        tok.no_truncation()
        tok.no_padding()
        self._tok = tok

    def count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids) if text else 0

    def count_many(self, texts: list[str]) -> list[int]:
        if not texts:
            return []
        encodings = self._tok.encode_batch(texts, add_special_tokens=False)
        return [len(e.ids) for e in encodings]

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Character (start, end) of every token, in order."""
        if not text:
            return []
        return [(a, b) for a, b in self._tok.encode(text, add_special_tokens=False).offsets]


@lru_cache(maxsize=1)
def get_tokenizer() -> Tokenizer:
    return Tokenizer()
