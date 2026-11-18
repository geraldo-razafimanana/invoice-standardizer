"""Geometry-aware text model: words -> lines -> cells (column-like groups of words)."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .numbers import fold, parse_number, parse_percent


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page: int
    conf: float | None = None  # OCR confidence 0-100, None for embedded text

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Cell:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return self.words[-1].x1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def h(self) -> float:
        return statistics.median(w.h for w in self.words)

    @property
    def conf(self) -> float | None:
        cs = [w.conf for w in self.words if w.conf is not None]
        return min(cs) if cs else None


@dataclass
class Line:
    page: int
    words: list[Word]
    cells: list[Cell] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def folded(self) -> str:
        return fold(self.text)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def h(self) -> float:
        return statistics.median(w.h for w in self.words)


_GROUP_RIGHT = re.compile(r"^\d{3}(?:[.,]\d+)?$")


def _merge_grouped_numbers(words: list[Word]) -> list[Word]:
    """Merge '1 234,56' (split by a normal space) back into a single number token."""
    out: list[Word] = []
    for w in words:
        if out:
            p = out[-1]
            gap = w.x0 - p.x1
            # previous token is "123" or an already merged "1 234"; next is "456" / "456,78"
            prev_is_group = bool(re.fullmatch(r"\d{1,3}(?: \d{3})*", p.text))
            if (
                prev_is_group
                and _GROUP_RIGHT.match(w.text)
                and -1 <= gap < 0.45 * max(p.h, w.h)
            ):
                out[-1] = Word(
                    p.x0, min(p.y0, w.y0), w.x1, max(p.y1, w.y1), p.text + " " + w.text, p.page,
                    None if p.conf is None or w.conf is None else min(p.conf, w.conf),
                )
                continue
        out.append(w)
    return out


def build_lines(words: list[Word], page: int) -> list[Line]:
    """Cluster words into visual lines (top to bottom) and split lines into cells."""
    words = [w for w in words if w.text.strip()]
    if not words:
        return []
    med_h = statistics.median(w.h for w in words)
    tol = max(2.0, 0.55 * med_h)
    ordered = sorted(words, key=lambda w: (w.cy, w.x0))
    groups: list[list[Word]] = []
    centers: list[float] = []
    for w in ordered:
        placed = False
        # compare with the most recent line(s) only
        for gi in range(len(groups) - 1, max(len(groups) - 3, -1), -1):
            if abs(w.cy - centers[gi]) <= tol:
                groups[gi].append(w)
                centers[gi] = statistics.fmean(x.cy for x in groups[gi])
                placed = True
                break
        if not placed:
            groups.append([w])
            centers.append(w.cy)
    lines: list[Line] = []
    for g in groups:
        g.sort(key=lambda w: w.x0)
        g = _merge_grouped_numbers(g)
        line = Line(page=page, words=g)
        line.cells = split_cells(g)
        lines.append(line)
    lines.sort(key=lambda l: l.y0)
    return lines


def split_cells(words: list[Word]) -> list[Cell]:
    if not words:
        return []
    h = statistics.median(w.h for w in words)
    threshold = 0.9 * h
    cells: list[list[Word]] = [[words[0]]]
    for prev, w in zip(words, words[1:]):
        if w.x0 - prev.x1 > threshold:
            cells.append([w])
        else:
            cells[-1].append(w)
    return [Cell(c) for c in cells]


def is_numeric_token(text: str) -> bool:
    return parse_number(text) is not None or parse_percent(text) is not None
