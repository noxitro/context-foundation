"""Markdown を、見出しの単位で分ける。

- 見出しから次の見出しの手前までを 1 つの単位にする。上位の見出しをスコープ(`設計 > 描画 > バッファ`)として付ける
- コードブロック(``` や ~~~)の中の `#` は見出しにしない。先頭の front matter(`---` で囲んだ部分)も同じ
- 短い節は、同じ階層か下の階層の隣とまとめる。長い節は、空行の切れ目で分ける
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import ChunkConfig
from .base import Chunk, ChunkStats, LineIndex, split_rows

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT_1 = re.compile(r"^ {0,3}=+[ \t]*$")
_SETEXT_2 = re.compile(r"^ {0,3}-{2,}[ \t]*$")
_NOT_SETEXT_TEXT = re.compile(r"^\s*(?:[-*+>|#]|\d+[.)]\s|`{3,}|~{3,})")


@dataclass
class _Section:
    r0: int
    r1: int
    level: int          # 0 は、最初の見出しより前の部分
    title: str
    scope: str


def _sections(li: LineIndex) -> list[_Section]:
    n = len(li)
    heads: list[tuple[int, int, str]] = []   # (row, level, title)
    fence: str | None = None
    r = 0
    if n and li.lines[0].strip() == "---":   # front matter
        for j in range(1, n):
            if li.lines[j].strip() in ("---", "..."):
                r = j + 1
                break
    fm_end = r
    while r < n:
        line = li.lines[r]
        m = _FENCE.match(line)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            r += 1
            continue
        if fence is None:
            m = _ATX.match(line)
            if m:
                heads.append((r, len(m.group(1)), (m.group(2) or "").strip()))
            elif (r + 1 < n and line.strip() and not _NOT_SETEXT_TEXT.match(line)
                  and (r == 0 or not li.lines[r - 1].strip())):
                nxt = li.lines[r + 1]
                if _SETEXT_1.match(nxt):
                    heads.append((r, 1, line.strip()))
                    r += 1
                elif _SETEXT_2.match(nxt):
                    heads.append((r, 2, line.strip()))
                    r += 1
        r += 1

    out: list[_Section] = []
    if not heads or heads[0][0] > 0:
        end = (heads[0][0] - 1) if heads else n - 1
        if li.trim(0, end):
            # 見出しの代わりに、front matter の後の最初の行を付ける(本文が無ければ、front matter の中の最初の行)
            title = li.first_line(min(fm_end, end), end) if fm_end <= end else ""
            if not title:
                title = next((s.strip() for s in li.lines[:end + 1] if s.strip() and s.strip() not in ("---", "...")), "")
            out.append(_Section(0, end, 0, title[:160], ""))
    stack: list[tuple[int, str]] = []
    for i, (row, level, title) in enumerate(heads):
        while stack and stack[-1][0] >= level:
            stack.pop()
        scope = " > ".join(t for _, t in stack if t)
        stack.append((level, title))
        end = (heads[i + 1][0] - 1) if i + 1 < len(heads) else n - 1
        out.append(_Section(row, end, level, title, scope))
    return out


def chunk_markdown(text: str, cfg: ChunkConfig) -> tuple[list[Chunk], ChunkStats]:
    li = LineIndex(text)
    stats = ChunkStats(lines=li.nonblank(0, len(li) - 1))
    chunks: list[Chunk] = []
    group: list[_Section] = []

    def flush() -> None:
        nonlocal group
        if not group:
            return
        t = li.trim(group[0].r0, group[-1].r1)
        if t:
            first = group[0]
            titles = [s.title for s in group if s.title and s.level]
            chunks.append(Chunk(start_line=t[0] + 1, end_line=t[1] + 1, kind="heading" if first.level else "text",
                                method="heading", scope=first.scope, signature=first.title or li.first_line(*t),
                                symbols=titles[:16], text=li.text(*t)))
        group = []

    for s in _sections(li):
        size = li.size(s.r0, s.r1)
        if size > cfg.max_bytes:
            flush()
            ranges = split_rows(li, s.r0, s.r1, cfg.max_bytes, 10 ** 9)
            if len(ranges) > 1 and li.size(*ranges[0]) < cfg.min_bytes:
                # 見出しだけの切れ端にしない(次の部分が上限を超えても、見出しは本文と一緒に置く)
                ranges[1] = (ranges[0][0], ranges[1][1])
                ranges.pop(0)
            for i, (a, b) in enumerate(ranges, 1):
                chunks.append(Chunk(start_line=a + 1, end_line=b + 1, kind="heading" if s.level else "text",
                                    method="heading", scope=s.scope, signature=s.title or li.first_line(a, b),
                                    symbols=[s.title] if s.title and s.level else [],
                                    part=f"{i}/{len(ranges)}" if len(ranges) > 1 else "", text=li.text(a, b)))
            continue
        if group:
            gsize = li.size(group[0].r0, group[-1].r1)
            same_branch = s.level >= group[0].level and group[0].level > 0
            if same_branch and (gsize < cfg.min_bytes or size < cfg.min_bytes) \
                    and li.size(group[0].r0, s.r1) <= cfg.max_bytes:
                group.append(s)
                continue
            flush()
        group = [s]
    flush()
    return chunks, stats
