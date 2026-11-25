"""Cuts transcribed pages into passages for search.

A passage stays inside one page (so it always has one page number to show), starts a new
one at every heading, and is about TARGET characters: big enough to hold an idea, small
enough that the closest passages to a question are about that question. A tiny tail is
joined to the passage before it. Each passage remembers the heading it sits under, carried
over from earlier pages, because "Phases of the Moon" says more about a paragraph than the
paragraph often does about itself.
"""

from dataclasses import dataclass

TARGET = 1200
MAX = 2000
MIN_TAIL = 200
HEADING_MAX = 200


@dataclass(frozen=True)
class Chunk:
    page: int
    order: int
    heading: str | None
    text: str

    def for_embedding(self) -> str:
        """What is embedded: the heading, then the passage."""
        return f"{self.heading}\n{self.text}" if self.heading else self.text


def chunk_pages(pages: list[tuple[int, str]], heading: str | None = None) -> list[Chunk]:
    """Passages of the given pages, in page order. `heading` is the one in force before
    the first page (from the batch before), so a read in parts cuts the same way."""
    out: list[Chunk] = []
    for page, text in sorted(pages, key=lambda p: p[0]):
        parts: list[tuple[str | None, str]] = []
        current: list[str] = []
        for block in _blocks(text):
            title = _heading_of(block)
            if title is not None:
                _close(parts, current, heading)
                heading = title
                continue
            for piece in _split_long(block):
                size = sum(len(c) + 1 for c in current)
                if current and size + len(piece) > TARGET:
                    _close(parts, current, heading)
                current.append(piece)
        _close(parts, current, heading)

        # join a tiny tail to the passage before it (same heading only)
        merged: list[tuple[str | None, str]] = []
        for h, body in parts:
            if merged and len(body) < MIN_TAIL and merged[-1][0] == h and len(merged[-1][1]) + len(body) < MAX:
                merged[-1] = (h, merged[-1][1] + "\n" + body)
            else:
                merged.append((h, body))
        out.extend(Chunk(page=page, order=i + 1, heading=h, text=body) for i, (h, body) in enumerate(merged))
    return out


def heading_after(pages: list[tuple[int, str]], before: str | None = None) -> str | None:
    """The heading in force after these pages (to start the next batch with)."""
    heading = before
    for _, text in sorted(pages, key=lambda p: p[0]):
        for block in _blocks(text):
            title = _heading_of(block)
            if title is not None:
                heading = title
    return heading


def _close(parts: list[tuple[str | None, str]], current: list[str], heading: str | None) -> None:
    body = "\n".join(current).strip()
    if body:
        parts.append((heading, body))
    current.clear()


def _blocks(text: str) -> list[str]:
    """Paragraphs; a heading line is always a block of its own."""
    blocks: list[str] = []
    para: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            if para:
                blocks.append(" ".join(para))
                para = []
            if line:
                blocks.append(line)
            continue
        para.append(line)
    if para:
        blocks.append(" ".join(para))
    return blocks


def _heading_of(block: str) -> str | None:
    if not block.startswith("#"):
        return None
    title = block.lstrip("#").strip()
    return title[:HEADING_MAX] or None


def _split_long(block: str) -> list[str]:
    """A paragraph longer than MAX, cut at sentence ends (or spaces) into pieces under it."""
    if len(block) <= MAX:
        return [block]
    pieces: list[str] = []
    rest = block
    while len(rest) > MAX:
        window = rest[:MAX]
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "), window.rfind("۔ "), window.rfind("؟ "))
        if cut < MAX // 2:
            cut = window.rfind(" ")
        if cut < MAX // 2:
            cut = MAX - 1
        pieces.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()
    if rest:
        pieces.append(rest)
    return pieces
