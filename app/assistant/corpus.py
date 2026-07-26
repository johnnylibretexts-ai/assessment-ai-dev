"""The baked-in knowledge the demo assistant answers from.

Deliberately not a retrieval system. The whole corpus is a few tens of
kilobytes against a million-token context window, so chunking and embedding it
would add real infrastructure and hand the model *less* than it can already
hold. Everything is loaded once and passed whole.

Every file here ships inside the image. Nothing in it may contain credentials,
tokens, password hashes, or private addresses -- ``tests/test_assistant.py``
enforces that, because this text is shown to anyone who can reach the app.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"


class CorpusError(RuntimeError):
    """Raised when the corpus is missing, so a broken image fails loudly."""


def corpus_files() -> tuple[Path, ...]:
    if not CORPUS_DIR.is_dir():
        raise CorpusError(f"assistant corpus directory is missing: {CORPUS_DIR}")
    return tuple(sorted(CORPUS_DIR.glob("*.md")))


@lru_cache(maxsize=1)
def corpus_text() -> str:
    """Return every corpus file concatenated, in filename order.

    Files are numbered so the ordering is stable and reviewable; the model sees
    the guide before the FAQ.
    """

    files = corpus_files()
    if not files:
        raise CorpusError(f"assistant corpus directory is empty: {CORPUS_DIR}")

    sections: list[str] = []
    for path in files:
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            raise CorpusError(f"assistant corpus file is empty: {path.name}")
        sections.append(f"<<< {path.name} >>>\n{body}")
    return "\n\n".join(sections)
