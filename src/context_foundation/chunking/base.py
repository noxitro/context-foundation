"""分割の共通部品: passage(Chunk)、行の扱い、短い単位をまとめる処理、行で分ける代替。

行番号は、外に出すときは 1 始まり・両端を含む。中では 0 始まりの行(row)で扱う。
passage は必ず行の単位で切る(`path:start-end` から同じ本文を取り出せるように)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import accumulate

from ..config import ChunkConfig


@dataclass
class Chunk:
    start_line: int                 # 1 始まり・含む
    end_line: int                   # 1 始まり・含む
    kind: str                       # function / class / namespace / heading / lines など
    method: str                     # 境界の決め方: syntax(構文)/ heading(見出し)/ lines(行)
    scope: str = ""                 # 外側のスコープ(名前空間・クラス、文書なら上位の見出し)
    signature: str = ""             # 関数やクラスの宣言の行、文書なら見出し
    symbols: list[str] = field(default_factory=list)   # この passage で定義している名前
    part: str = ""                  # 長い単位を分けたときの "2/3"
    text: str = ""


@dataclass
class Item:
    """まとめる前の 1 単位(構文の 1 ノードか、その一部)。"""
    r0: int
    r1: int
    kind: str
    scope: str = ""
    signature: str = ""
    name: str = ""
    symbols: list[str] = field(default_factory=list)
    fallback: bool = False          # 解析できなかった範囲(行で分ける)
    method: str = "syntax"
    head: bool = False              # 入れ物の見出し(`class Foo : Bar {` まで)。まとめた passage の代表にする


@dataclass
class ChunkStats:
    lines: int = 0                  # 空でない行の数
    fallback_lines: int = 0         # そのうち、行で分ける代替に落ちた行
    inner_error_units: int = 0      # 中に解析の誤りがあるが、境界は構文のまま使った単位
    parse_failed: bool = False      # ファイル全体を行で分けた


class LineIndex:
    """行ごとの文字数と本文。row は 0 始まり。"""

    def __init__(self, text: str):
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        self.lines = lines
        # 行の大きさは UTF-8 のバイト数(+ 改行の 1)。日本語は 1 文字が 3 になる
        self._cum = [0] + list(accumulate(len(s.encode("utf-8")) + 1 for s in lines))

    def __len__(self) -> int:
        return len(self.lines)

    def size(self, r0: int, r1: int) -> int:
        """row r0〜r1(含む)の大きさ(UTF-8 のバイト数)。"""
        if r1 < r0:
            return 0
        return self._cum[min(r1, len(self.lines) - 1) + 1] - self._cum[max(r0, 0)]

    def text(self, r0: int, r1: int) -> str:
        return "\n".join(self.lines[r0:r1 + 1])

    def blank(self, r: int) -> bool:
        return not self.lines[r].strip()

    def trim(self, r0: int, r1: int) -> tuple[int, int] | None:
        """両端の空行を除いた範囲。全部が空行なら None。"""
        r0, r1 = max(r0, 0), min(r1, len(self.lines) - 1)
        while r0 <= r1 and self.blank(r0):
            r0 += 1
        while r1 >= r0 and self.blank(r1):
            r1 -= 1
        return (r0, r1) if r0 <= r1 else None

    def nonblank(self, r0: int, r1: int) -> int:
        return sum(1 for r in range(max(r0, 0), min(r1, len(self.lines) - 1) + 1) if not self.blank(r))

    def first_line(self, r0: int, r1: int, limit: int = 160) -> str:
        for r in range(r0, r1 + 1):
            s = self.lines[r].strip()
            if s:
                return s[:limit]
        return ""


def split_rows(li: LineIndex, r0: int, r1: int, max_bytes: int, max_lines: int) -> list[tuple[int, int]]:
    """row r0〜r1 を、大きさと行数の上限で分ける。切れ目は、後ろ半分にある空行を優先する。"""
    out: list[tuple[int, int]] = []
    start = r0
    while start <= r1:
        end = start
        while end < r1 and (end - start + 1) < max_lines and li.size(start, end + 1) <= max_bytes:
            end += 1
        if end < r1:
            # 空行で切れるなら、そこまで戻る(戻りすぎて小さな断片にしない)
            floor = start + (end - start) // 2
            cut = end
            while cut > floor and not li.blank(cut):
                cut -= 1
            if cut > floor:
                end = cut
        t = li.trim(start, end)
        if t:
            out.append(t)
        start = end + 1
    return out


# まとめた単位の種類を決めるときの優先順(大きいほど優先)
_KIND_RANK = {
    "function": 9, "method": 9, "constructor": 9, "destructor": 9, "operator": 9,
    "class": 8, "struct": 8, "interface": 8, "record": 8, "union": 8,
    "enum": 7, "property": 6, "namespace": 5, "macro": 4, "typedef": 4, "concept": 4, "delegate": 4, "event": 4,
    "field": 3, "declaration": 3, "statement": 2, "using": 1, "include": 1, "preproc": 1, "comment": 0,
}


def _uniq(names: list[str], limit: int = 16) -> list[str]:
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
            if len(out) >= limit:
                break
    return out


def _chunk_from_items(group: list[Item], li: LineIndex) -> Chunk | None:
    t = li.trim(group[0].r0, max(i.r1 for i in group))
    if not t:
        return None
    r0, r1 = t
    # 代表(種類・スコープ・シグネチャを借りる単位): 入れ物の見出しがあればそれ。無ければ、定義のうち最も大きいもの
    heads = [i for i in group if i.head]
    defs = [i for i in group if _KIND_RANK.get(i.kind, 2) >= 2]
    primary = heads[0] if heads else max(defs or group, key=lambda i: (li.size(i.r0, i.r1), -i.r0))
    names: list[str] = []
    for i in group:
        if i.name:
            names.append(i.name)
        names.extend(i.symbols)
    return Chunk(start_line=r0 + 1, end_line=r1 + 1, kind=primary.kind, method=primary.method, scope=primary.scope,
                 signature=primary.signature or li.first_line(r0, r1), symbols=_uniq(names), text=li.text(r0, r1))


def pack_items(items: list[Item], li: LineIndex, cfg: ChunkConfig, stats: ChunkStats | None = None) -> list[Chunk]:
    """隣り合う単位を、短いものだけまとめて passage にする。

    - どちらかが min_bytes より短く、合わせて max_bytes に収まるなら、まとめる
    - 同じ行を分け合う単位は、必ずまとめる
    - 解析できなかった範囲(fallback)は、隣どうしをつないでから、行で分ける
    """
    chunks: list[Chunk] = []
    group: list[Item] = []
    fb: list[Item] = []

    def flush_group() -> None:
        nonlocal group
        if group:
            c = _chunk_from_items(group, li)
            if c:
                chunks.append(c)
            group = []

    def flush_fallback() -> None:
        nonlocal fb
        if fb:
            r0, r1 = fb[0].r0, max(i.r1 for i in fb)
            for a, b in split_rows(li, r0, r1, cfg.max_bytes, cfg.line_window):
                chunks.append(Chunk(start_line=a + 1, end_line=b + 1, kind="lines", method="lines", scope=fb[0].scope,
                                    signature=li.first_line(a, b), text=li.text(a, b)))
                if stats is not None:
                    stats.fallback_lines += li.nonblank(a, b)
            fb = []

    for it in sorted(items, key=lambda i: (i.r0, i.r1)):
        if it.fallback:
            if group and it.r0 <= max(i.r1 for i in group):
                # 同じ行を分け合っている: 構文の単位ごと、行で分ける側に回す
                fb = [Item(r0=group[0].r0, r1=max(i.r1 for i in group), kind="lines", scope=group[0].scope,
                           fallback=True)]
                group = []
            flush_group()
            fb.append(it)
            continue
        if fb:
            if it.r0 <= max(i.r1 for i in fb):
                fb.append(Item(r0=it.r0, r1=it.r1, kind="lines", scope=it.scope, fallback=True))
                continue
            flush_fallback()
        if not group:
            group = [it]
            continue
        g0, g1 = group[0].r0, max(i.r1 for i in group)
        if it.r0 <= g1:
            group.append(it)
            continue
        gsize, isize = li.size(g0, g1), li.size(it.r0, it.r1)
        if (gsize < cfg.min_bytes or isize < cfg.min_bytes) and li.size(g0, it.r1) <= cfg.max_bytes:
            group.append(it)
        else:
            flush_group()
            group = [it]
    flush_group()
    flush_fallback()
    return chunks


def split_oversized(chunks: list[Chunk], li: LineIndex, cfg: ChunkConfig) -> list[Chunk]:
    """上限を大きく超えた passage(巨大なコメント、同じ行を分け合ってつながった単位など)を、行で分け直す。
    種類・スコープ・シグネチャはそのまま引き継ぎ、どの部分かを part に書く。"""
    limit = cfg.max_bytes + cfg.min_bytes
    out: list[Chunk] = []
    for c in chunks:
        if c.part or c.end_line == c.start_line or li.size(c.start_line - 1, c.end_line - 1) <= limit:
            out.append(c)
            continue
        ranges = split_rows(li, c.start_line - 1, c.end_line - 1, cfg.max_bytes, 10 ** 9)
        for i, (a, b) in enumerate(ranges, 1):
            out.append(Chunk(start_line=a + 1, end_line=b + 1, kind=c.kind, method=c.method, scope=c.scope,
                             signature=c.signature, symbols=list(c.symbols),
                             part=f"{i}/{len(ranges)}" if len(ranges) > 1 else "", text=li.text(a, b)))
    return out


# 定義ではない断片(単独で passage にしても意味が薄いもの)
_FRAGMENT_KINDS = {"namespace", "preproc", "include", "using", "comment"}


def merge_tiny(chunks: list[Chunk], li: LineIndex, cfg: ChunkConfig) -> list[Chunk]:
    """定義を含まない短い断片を、隣の passage に入れる。

    入れ物に下りたときに残る、見出しだけの断片(`namespace Foo {` の 1 行)や、末尾の `#endif` を、
    1 行だけの passage にしないための後始末。関数や型の定義は、短くても別のスコープの passage には入れない
    (スコープとシグネチャが食い違うため)。
    """
    def size(c: Chunk) -> int:
        return li.size(c.start_line - 1, c.end_line - 1)

    def adjacent(a: Chunk, b: Chunk) -> bool:
        return not (b.start_line - 1 > a.end_line and li.trim(a.end_line, b.start_line - 2))

    def fits(a: Chunk, b: Chunk) -> bool:
        return li.size(a.start_line - 1, b.end_line - 1) <= cfg.max_bytes + cfg.min_bytes

    out: list[Chunk] = []
    pending = sorted(chunks, key=lambda c: (c.start_line, c.end_line))
    i = 0
    while i < len(pending):
        c = pending[i]
        if c.kind in _FRAGMENT_KINDS and c.method == "syntax" and not c.part and size(c) < cfg.min_bytes:
            nxt = pending[i + 1] if i + 1 < len(pending) else None
            if nxt is not None and nxt.method == "syntax" and not nxt.part and adjacent(c, nxt) and fits(c, nxt):
                nxt.symbols = _uniq(c.symbols + nxt.symbols)       # 後ろの passage の頭に入れる
                nxt.start_line = c.start_line
                nxt.text = li.text(nxt.start_line - 1, nxt.end_line - 1)
                i += 1
                continue
            if out and c.kind in ("preproc", "comment") and adjacent(out[-1], c) and fits(out[-1], c):
                out[-1].end_line = c.end_line                      # 前の passage の末尾に入れる
                out[-1].text = li.text(out[-1].start_line - 1, out[-1].end_line - 1)
                i += 1
                continue
        out.append(c)
        i += 1
    return out


def cover_gaps(chunks: list[Chunk], li: LineIndex, cfg: ChunkConfig, stats: ChunkStats | None = None) -> list[Chunk]:
    """どの passage にも入らなかった空でない行を、落とさずに拾う。

    解析の前に伏せたマクロの行や、文法が拾わなかった行のための後始末。短い隙間はすぐ後ろ(無ければ前)の
    passage に入れ、長い隙間は行で分けた passage にする。
    """
    chunks = sorted(chunks, key=lambda c: (c.start_line, c.end_line))
    out: list[Chunk] = []

    def lines_chunks(r0: int, r1: int, scope: str) -> None:
        for a, b in split_rows(li, r0, r1, cfg.max_bytes, cfg.line_window):
            out.append(Chunk(start_line=a + 1, end_line=b + 1, kind="lines", method="lines", scope=scope,
                             signature=li.first_line(a, b), text=li.text(a, b)))
            if stats is not None:
                stats.fallback_lines += li.nonblank(a, b)

    prev_end = 0  # 直前の passage の最後の行(1 始まり)。まだ無ければ 0
    for c in chunks:
        if c.start_line <= prev_end:           # 重なり(起きないはずだが、念のため前を縮める)
            if c.end_line <= prev_end:
                continue
            c.start_line = prev_end + 1
            c.text = li.text(c.start_line - 1, c.end_line - 1)
        gap = li.trim(prev_end, c.start_line - 2) if c.start_line - 1 > prev_end else None
        if gap:
            if li.size(gap[0], gap[1]) > cfg.min_bytes:
                lines_chunks(gap[0], gap[1], c.scope)
            else:
                c.start_line = gap[0] + 1
                c.text = li.text(c.start_line - 1, c.end_line - 1)
        out.append(c)
        prev_end = c.end_line
    tail = li.trim(prev_end, len(li) - 1) if len(li) > prev_end else None
    if tail:
        if out and li.size(tail[0], tail[1]) <= cfg.min_bytes:
            last = out[-1]
            last.end_line = tail[1] + 1
            last.text = li.text(last.start_line - 1, last.end_line - 1)
        else:
            lines_chunks(tail[0], tail[1], out[-1].scope if out else "")
    return out


def chunk_lines(text: str, cfg: ChunkConfig, scope: str = "") -> tuple[list[Chunk], ChunkStats]:
    """行で分ける(構文を見ない)。"""
    li = LineIndex(text)
    stats = ChunkStats(lines=li.nonblank(0, len(li) - 1))
    chunks = [Chunk(start_line=a + 1, end_line=b + 1, kind="lines", method="lines", scope=scope,
                    signature=li.first_line(a, b), text=li.text(a, b))
              for a, b in split_rows(li, 0, len(li) - 1, cfg.max_bytes, cfg.line_window)] if len(li) else []
    return chunks, stats
