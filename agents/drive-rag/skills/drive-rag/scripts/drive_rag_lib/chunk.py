"""Structure-preserving, deterministic chunk construction."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

from .models import Chunk, ExtractedDocument
from .protocol import DriveRagError


DEFAULT_MAX_TOKENS = 700
DEFAULT_OVERLAP_TOKENS = 100
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n{2,}")


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


def chunk_document(
    document: ExtractedDocument,
    token_counter: TokenCounter,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> tuple[Chunk, ...]:
    """Chunk each native block without crossing its source locator."""

    if not isinstance(document, ExtractedDocument):
        raise DriveRagError("document must be extracted content", code="INVALID_EXTRACTION")
    if not document.file_id.strip() or not document.revision.strip():
        raise DriveRagError(
            "extracted file identity must not be empty", code="INVALID_EXTRACTION"
        )
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
        or not isinstance(overlap_tokens, int)
        or isinstance(overlap_tokens, bool)
        or overlap_tokens < 0
        or overlap_tokens >= max_tokens
    ):
        raise DriveRagError("invalid chunk token limits", code="INVALID_EXTRACTION")

    chunks: list[Chunk] = []
    ordinal = 0
    for block in document.blocks:
        locator = block.locator.strip()
        text = _normalize_text(block.text)
        if not locator or not text:
            continue
        block_chunks = (
            _split_tabular(text, token_counter, max_tokens, overlap_tokens)
            if locator.startswith("sheet:") and "\n" in text
            else _split_text(text, token_counter, max_tokens, overlap_tokens)
        )
        for chunk_text in block_chunks:
            digest = hashlib.sha256(
                f"{document.file_id}\0{document.revision}\0{locator}\0{ordinal}\0{chunk_text}".encode()
            ).hexdigest()
            chunks.append(
                Chunk(
                    f"{document.file_id}:{digest}",
                    chunk_text,
                    locator,
                    dict(block.metadata),
                )
            )
            ordinal += 1
    return tuple(chunks)


def _split_text(
    text: str,
    token_counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    _count(token_counter, "")
    result: list[str] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        end = _largest_end(text, start, token_counter, max_tokens)
        if end == start:
            raise DriveRagError(
                "token counter cannot fit a single character",
                code="INVALID_EXTRACTION",
            )
        if end < len(text):
            end = _preferred_boundary(text, start, end, token_counter, max_tokens)
        chunk = text[start:end].strip()
        if not chunk:
            raise DriveRagError(
                "token counter produced an empty chunk", code="INVALID_EXTRACTION"
            )
        result.append(chunk)
        if end >= len(text):
            break
        next_start = _overlap_start(
            text, start, end, token_counter, overlap_tokens
        )
        if next_start <= start:
            next_start = end
        start = next_start
    return result


def _split_tabular(
    text: str,
    token_counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _split_text(text, token_counter, max_tokens, overlap_tokens)
    header, rows = lines[0], lines[1:]
    if _count(token_counter, header) >= max_tokens:
        return _split_text(text, token_counter, max_tokens, overlap_tokens)

    chunks: list[str] = []
    current_rows: list[str] = []
    for row in rows:
        candidate = "\n".join([header, *current_rows, row])
        if _count(token_counter, candidate) <= max_tokens:
            current_rows.append(row)
            continue
        if current_rows:
            chunks.append("\n".join([header, *current_rows]))
            current_rows = []
        single_row = f"{header}\n{row}"
        if _count(token_counter, single_row) <= max_tokens:
            current_rows.append(row)
            continue
        chunks.extend(
            _split_with_prefix(
                row,
                f"{header}\n",
                token_counter,
                max_tokens,
                overlap_tokens,
            )
        )
    if current_rows:
        chunks.append("\n".join([header, *current_rows]))
    return chunks or [header]


def _split_with_prefix(
    text: str,
    prefix: str,
    token_counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        low, high, accepted = start + 1, len(text), start
        while low <= high:
            middle = (low + high) // 2
            candidate = f"{prefix}{text[start:middle]}"
            if _count(token_counter, candidate) <= max_tokens:
                accepted = middle
                low = middle + 1
            else:
                high = middle - 1
        if accepted == start:
            raise DriveRagError(
                "sheet header leaves no token budget for a row",
                code="INVALID_EXTRACTION",
            )
        chunks.append(f"{prefix}{text[start:accepted]}")
        if accepted >= len(text):
            break
        next_start = _overlap_start(
            text, start, accepted, token_counter, overlap_tokens
        )
        if next_start <= start:
            next_start = accepted
        start = next_start
    return chunks


def _largest_end(
    text: str, start: int, token_counter: TokenCounter, max_tokens: int
) -> int:
    low, high, accepted = start + 1, len(text), start
    while low <= high:
        middle = (low + high) // 2
        if _count(token_counter, text[start:middle].strip()) <= max_tokens:
            accepted = middle
            low = middle + 1
        else:
            high = middle - 1
    return accepted


def _preferred_boundary(
    text: str,
    start: int,
    maximum_end: int,
    token_counter: TokenCounter,
    max_tokens: int,
) -> int:
    candidate = text[start:maximum_end]
    minimum_fill = max(1, max_tokens // 2)
    sentence_ends = [match.start() for match in _SENTENCE_BOUNDARY.finditer(candidate)]
    for relative_end in reversed(sentence_ends):
        end = start + relative_end
        if end > start and _count(token_counter, text[start:end].strip()) >= minimum_fill:
            return end
    whitespace = [match.start() for match in re.finditer(r"\s+", candidate)]
    if whitespace:
        end = start + whitespace[-1]
        if end > start:
            return end
    return maximum_end


def _overlap_start(
    text: str,
    start: int,
    end: int,
    token_counter: TokenCounter,
    overlap_tokens: int,
) -> int:
    if overlap_tokens == 0:
        return end
    low, high, accepted = start, end, end
    while low <= high:
        middle = (low + high) // 2
        if _count(token_counter, text[middle:end].strip()) <= overlap_tokens:
            accepted = middle
            high = middle - 1
        else:
            low = middle + 1
    if any(character.isspace() for character in text[start:end]):
        boundary = re.search(r"\s+", text[accepted:end])
        if boundary is not None and accepted + boundary.start() > start:
            accepted += boundary.end()
    while accepted < end and text[accepted].isspace():
        accepted += 1
    return accepted


def _count(token_counter: TokenCounter, text: str) -> int:
    try:
        count = token_counter.count(text)
    except Exception as exc:
        raise DriveRagError("token counter failed", code="INVALID_EXTRACTION") from exc
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise DriveRagError(
            "token counter returned an invalid count", code="INVALID_EXTRACTION"
        )
    return count


def _normalize_text(text: str) -> str:
    return re.sub(r" +", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
