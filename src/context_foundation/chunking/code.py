"""C・C++・C# を、構文の単位(関数・クラス・名前空間など)で分ける。解析は tree-sitter。

方針
- クラスなどの型は、上限に収まれば 1 つの passage にし、収まらなければ中の単位に下りる。名前空間には必ず下りる。
  下りたときは、中の passage に外側のスコープ(`AZ::RHI::BufferPool` など)を付ける
- 関数は 1 つの passage。上限を超える関数は、中の文の切れ目で分け、どの部分にも同じシグネチャを付ける
- 短い単位(1 行の宣言や小さな関数)は、同じ入れ物の中の隣とまとめる
- 直前のコメント(説明)は、その単位に含める
- 解析できなかった範囲(マクロで文法が崩れた所)は、行で分ける代替に落とし、その行数を数える

C・C++ では、解析の前に「属性のようなマクロ」を同じ長さの空白に置き換える(行と桁は変わらない)。
`class ENGINE_API Foo : public Bar` の `ENGINE_API` や、`UCLASS(...)` のように 1 行を占める呼び出しは、
そのままだと文法が崩れてクラス全体が解析できなくなるため。passage の本文は元のままを使う。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from ..config import ChunkConfig
from .base import (Chunk, ChunkStats, Item, LineIndex, chunk_lines, cover_gaps, merge_tiny, pack_items,
                   split_oversized, split_rows)


@dataclass(frozen=True)
class LangSpec:
    name: str
    sep: str                                  # スコープの区切り
    containers: dict[str, str]                # 入れ物(スコープになる)ノード → 種類
    transparent: frozenset[str]               # スコープにならない入れ物(#if の塊、extern "C" など)
    functions: dict[str, str]                 # 関数のようなノード → 種類
    kinds: dict[str, str]                     # そのほかのノード → 種類
    comments: frozenset[str] = frozenset({"comment"})


_C_KINDS = {
    "enum_specifier": "enum", "preproc_def": "macro", "preproc_function_def": "macro", "preproc_include": "include",
    "preproc_call": "preproc", "type_definition": "typedef", "alias_declaration": "typedef",
    "using_declaration": "using", "namespace_alias_definition": "using", "declaration": "declaration",
    "field_declaration": "field", "concept_definition": "concept", "expression_statement": "statement",
    "struct_specifier": "struct", "union_specifier": "union", "class_specifier": "class",
}
_PREPROC_BLOCKS = frozenset({"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef"})

SPECS: dict[str, LangSpec] = {
    "c": LangSpec(
        name="c", sep="::",
        containers={"struct_specifier": "struct", "union_specifier": "union"},
        transparent=_PREPROC_BLOCKS | {"linkage_specification"},
        functions={"function_definition": "function"},
        kinds=_C_KINDS),
    "cpp": LangSpec(
        name="cpp", sep="::",
        containers={"namespace_definition": "namespace", "class_specifier": "class", "struct_specifier": "struct",
                    "union_specifier": "union"},
        transparent=_PREPROC_BLOCKS | {"linkage_specification"},
        functions={"function_definition": "function"},
        kinds=_C_KINDS),
    "csharp": LangSpec(
        name="csharp", sep=".",
        containers={"namespace_declaration": "namespace", "class_declaration": "class",
                    "struct_declaration": "struct", "interface_declaration": "interface",
                    "record_declaration": "record"},
        transparent=_PREPROC_BLOCKS,
        functions={"method_declaration": "method", "constructor_declaration": "constructor",
                   "destructor_declaration": "destructor", "operator_declaration": "operator",
                   "conversion_operator_declaration": "operator", "local_function_statement": "function",
                   "property_declaration": "property", "indexer_declaration": "property",
                   "event_declaration": "event"},
        kinds={"enum_declaration": "enum", "delegate_declaration": "delegate", "field_declaration": "field",
               "event_field_declaration": "event", "using_directive": "using", "global_statement": "statement",
               "file_scoped_namespace_declaration": "namespace"}),
}

# C・C++: 中に本体のある型を包む宣言(`struct A {...} a;`、`typedef struct {...} A;`、入れ子の `struct Inner {...};`)
_C_WRAPPERS = frozenset({"template_declaration", "declaration", "field_declaration", "type_definition"})

# 属性のようなマクロ(解析の前に伏せる)
_EXPORT_MACRO = re.compile(r"\b(class|struct|union)(\s+)([A-Z][A-Z0-9_]{2,})(?=\s+[A-Za-z_]\w*(?:\s+final)?\s*[:{])")
# 行の頭から始まる、大文字だけの名前の呼び出し(`UCLASS(...)`、`AZ_ENUM_CLASS(...` など)
_MACRO_CALL = re.compile(r"^[ \t]*([A-Z][A-Z0-9_]{2,})[ \t]*\(", re.MULTILINE)
_MACRO_SCAN_LIMIT = 20_000

_runtime: dict[str, tuple] = {}


def _load(lang: str):
    """言語ごとの (Parser, 誤りを探す Query) を、最初に使うときに作る。"""
    if lang not in _runtime:
        from tree_sitter import Language, Parser, Query
        if lang == "c":
            import tree_sitter_c as mod
        elif lang == "cpp":
            import tree_sitter_cpp as mod
        elif lang == "csharp":
            import tree_sitter_c_sharp as mod
        else:
            raise KeyError(lang)
        language = Language(mod.language())
        _runtime[lang] = (Parser(language), Query(language, "(ERROR) @e"))
    return _runtime[lang]


def _spaces(text: str) -> str:
    """改行と ASCII でない文字はそのまま、ほかを空白にする(行の数もバイト数も変えない)。"""
    return "".join(c if c == "\n" or c > "\x7f" else " " for c in text)


def _matching_paren(text: str, open_at: int) -> int:
    """open_at の `(` に対応する `)` の位置。文字列の中のかっこは数えない。見つからなければ -1。"""
    depth, i, end = 0, open_at, min(len(text), open_at + _MACRO_SCAN_LIMIT)
    while i < end:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < end and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _blank_macro_calls(text: str) -> str:
    """文として単独で置かれた、大文字だけの名前のマクロ呼び出し(セミコロン無し)を空白にする。

    `UCLASS(...)` や `GENERATED_BODY()` のように 1 行のものも、`AZ_ENUM_CLASS(Name, (A, 0), ...)` のように
    何行にもわたるものも対象。そのままだと文法が崩れて、後ろに続く宣言まで解析できなくなる。伏せた行は
    どの構文の単位にも入らないので、後で「行で分ける passage」として拾われる(短ければ隣の単位に入る)。
    次が `{` のもの(`TEST_F(A, B) {`)は、関数の頭として働くので、そのままにする。
    """
    out: list[str] = []
    pos = 0
    for m in _MACRO_CALL.finditer(text):
        if m.start() < pos:
            continue
        close = _matching_paren(text, m.end() - 1)
        if close < 0:
            continue
        eol = text.find("\n", close)
        eol = len(text) if eol < 0 else eol
        if text[close + 1:eol].strip():             # `)` の後ろに、同じ行で何か続く(`;` など)
            continue
        if text[eol:eol + 200].lstrip().startswith("{"):
            continue
        start = m.start(1)
        out.append(text[pos:start])
        out.append(_spaces(text[start:close + 1]))
        pos = close + 1
    out.append(text[pos:])
    return "".join(out)


def sanitize_for_parse(text: str, cfg: ChunkConfig) -> str:
    """属性のようなマクロを、同じ長さの空白に置き換える(ASCII だけを置き換えるので、行もバイト数も変わらない)。"""
    out = _EXPORT_MACRO.sub(lambda m: m.group(1) + m.group(2) + " " * len(m.group(3)), text)
    out = _blank_macro_calls(out)
    for pat in cfg.attribute_macros:
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        out = rx.sub(lambda m: _spaces(m.group(0)), out)
    return out if len(out) == len(text) else text


def _collapse(s: str, limit: int = 240) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


class _Ctx:
    def __init__(self, li: LineIndex, cfg: ChunkConfig, spec: LangSpec, src: bytes, error_starts: list[int],
                 stats: ChunkStats):
        self.li, self.cfg, self.spec, self.src, self.stats = li, cfg, spec, src, stats
        self.error_starts = error_starts
        self.last_row = len(li) - 1

    # --- 小さな道具 ---
    def rows(self, node) -> tuple[int, int]:
        r0, r1 = node.start_point.row, node.end_point.row
        if node.end_point.column == 0 and r1 > r0:   # 改行までを含むノード(#define など)
            r1 -= 1
        return min(r0, self.last_row), min(r1, self.last_row)

    def text(self, node) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def error_in(self, a: int, b: int) -> bool:
        i = bisect.bisect_left(self.error_starts, a)
        return i < len(self.error_starts) and self.error_starts[i] < b

    def join(self, scope: str, name: str) -> str:
        if not name:
            return scope
        return f"{scope}{self.spec.sep}{name}" if scope else name

    def signature(self, start_byte: int, node, body) -> str:
        end = body.start_byte if body is not None else node.end_byte
        for c in node.children:
            if c.type == "field_initializer_list" and start_byte < c.start_byte < end:
                end = c.start_byte   # コンストラクタの初期化リストは外す
        s = _collapse(self.src[start_byte:end].decode("utf-8", "replace"))
        return s.rstrip("{").rstrip()

    def name_of(self, node) -> str:
        """宣言の名前(分からなければ空)。C++ の `A::f` のような修飾名は、そのまま返す。"""
        n = node.child_by_field_name("name")
        if n is not None:
            return "".join(self.text(n).split())[:120]
        d = node.child_by_field_name("declarator")
        for _ in range(10):
            if d is None:
                break
            nxt = d.child_by_field_name("declarator")
            if nxt is None:
                if d.type in ("reference_declarator", "pointer_declarator") and d.named_child_count:
                    nxt = d.named_children[-1]
                else:
                    break
            d = nxt
        if d is not None and d.type in ("identifier", "field_identifier", "qualified_identifier", "destructor_name",
                                        "operator_name", "type_identifier", "template_function", "operator_cast"):
            return "".join(self.text(d).split())[:120]
        if self.spec.name == "csharp":
            for c in node.named_children:
                if c.type == "variable_declaration":
                    for v in c.named_children:
                        if v.type == "variable_declarator":
                            return self.name_of(v) or (self.text(v.named_children[0]) if v.named_child_count else "")
        return ""

    def unwrap(self, node):
        """template などの包みを外して、中の宣言を返す(外せなければ None)。"""
        if self.spec.name == "csharp" or node.type not in _C_WRAPPERS:
            return None
        if node.type == "template_declaration":
            for c in reversed(node.named_children):
                if c.type not in ("template_parameter_list", "requires_clause", "comment"):
                    return c
            return None
        t = node.child_by_field_name("type")
        if t is not None and t.type in self.spec.containers and t.child_by_field_name("body") is not None:
            return t
        return None

    def member_names(self, body, limit: int = 12) -> list[str]:
        names: list[str] = []
        for c in body.named_children[:60]:
            inner = c
            for _ in range(3):
                u = self.unwrap(inner)
                if u is None:
                    break
                inner = u
            if inner.type in self.spec.functions or inner.type in self.spec.containers or inner.type in (
                    "declaration", "field_declaration", "enum_specifier", "enum_declaration"):
                n = self.name_of(inner)
                if n and n not in names:
                    names.append(n)
                    if len(names) >= limit:
                        break
        return names

    # --- 走査 ---
    def walk(self, nodes, scope: str, out: list) -> None:
        """兄弟のノードを順に処理し、out に単位(Item)か、できあがった passage の並びを足す。"""
        pending: tuple[int, int] | None = None   # まだどの単位にも付けていない、連続したコメント
        for node in nodes:
            r0, r1 = self.rows(node)
            if node.type in self.spec.comments:
                last = out[-1] if out else None
                if pending is None and last is not None:
                    if isinstance(last, Item) and r0 <= last.r1:      # 行末のコメント
                        last.r1 = max(last.r1, r1)
                        continue
                    if isinstance(last, list) and last and r0 <= last[-1].end_line - 1:
                        continue
                if pending is not None and r0 <= pending[1] + 1:
                    pending = (pending[0], max(pending[1], r1))
                else:
                    if pending is not None:
                        out.append(Item(pending[0], pending[1], "comment", scope))
                    pending = (r0, r1)
                continue
            start = r0
            if pending is not None:
                if r0 <= pending[1] + 1:
                    start = pending[0]            # 直前のコメントは、この単位に含める
                else:
                    out.append(Item(pending[0], pending[1], "comment", scope))
                pending = None
            if node.type == "file_scoped_namespace_declaration":   # C#: 以後の兄弟がこの名前空間に入る
                name = self.name_of(node)
                out.append(Item(start, r1, "namespace", scope, self.signature(node.start_byte, node, None), name))
                scope = self.join(scope, name)
                continue
            self.node(node, start, r0, r1, scope, out)
        if pending is not None:
            out.append(Item(pending[0], pending[1], "comment", scope))

    def node(self, node, start: int, r0: int, r1: int, scope: str, out: list) -> None:
        spec, cfg, li = self.spec, self.cfg, self.li
        if node.type == "ERROR" or node.is_missing:
            self.unparsed(start, r1, scope, out)
            return
        inner = node
        for _ in range(4):
            u = self.unwrap(inner)
            if u is None:
                break
            inner = u
        it = inner.type
        size = li.size(start, r1)

        if it in spec.transparent:
            self.transparent(inner, start, r0, r1, scope, out)
            return

        if it in spec.containers and inner.child_by_field_name("body") is not None:
            body = inner.child_by_field_name("body")
            kind, name = spec.containers[it], self.name_of(inner)
            sig = self.signature(node.start_byte, inner, body)
            head_error = node.has_error and self.error_in(node.start_byte, body.start_byte)
            if head_error:
                out.append(Item(start, r1, "lines", scope, fallback=True))
                return
            kids = body.named_children
            # 名前空間は、それ自体は意味のある単位ではないので、小さくても中に下りる(中の型や関数を単位にする)。
            # クラスなどの型は、上限に収まれば 1 つの単位にする
            if (size <= cfg.max_bytes and kind != "namespace") or not kids:
                if node.has_error and self.error_in(body.start_byte, node.end_byte):
                    self.stats.inner_error_units += 1
                out.append(Item(start, r1, kind, scope, sig, name, symbols=self.member_names(body)))
                return
            inner_scope = self.join(scope, name)
            sub: list = []
            first = self.rows(kids[0])[0]
            if first > start:                      # 見出しの行(`class Foo : Bar {` まで)
                sub.append(Item(start, first - 1, kind, inner_scope, sig, name, head=kind != "namespace"))
            self.walk(kids, inner_scope, sub)
            chunks = self.finish(sub)
            if chunks:
                if first <= start:                 # 見出しと最初のメンバーが同じ行にある
                    chunks[0].start_line = min(chunks[0].start_line, start + 1)
                    chunks[0].text = li.text(chunks[0].start_line - 1, chunks[0].end_line - 1)
                last = chunks[-1]
                if r1 + 1 > last.end_line:         # 閉じる行(`};`)を最後の passage に含める
                    last.end_line = r1 + 1
                    last.text = li.text(last.start_line - 1, last.end_line - 1)
                out.append(chunks)
            else:
                out.append(Item(start, r1, kind, scope, sig, name))
            return

        if it in spec.functions:
            body = inner.child_by_field_name("body") or inner.child_by_field_name("accessors")
            kind = spec.functions[it]
            full = self.name_of(inner)
            prefix, _, name = full.rpartition("::") if spec.sep == "::" else ("", "", full)
            fscope = self.join(scope, prefix)
            sig = self.signature(node.start_byte, inner, body)
            head_end = body.start_byte if body is not None else node.end_byte
            if node.has_error and self.error_in(node.start_byte, head_end):
                out.append(Item(start, r1, "lines", scope, fallback=True))
                return
            if node.has_error:
                self.stats.inner_error_units += 1
            if size <= cfg.max_bytes:
                out.append(Item(start, r1, kind, fscope, sig, name or full))
            else:
                out.append(self.parts(inner, start, r1, kind, fscope, sig, name or full))
            return

        kind = spec.kinds.get(it, "declaration")
        if node.has_error and self.error_in(node.start_byte, node.end_byte):
            self.unparsed(start, r1, scope, out)
            return
        name = self.name_of(inner)
        sig = _collapse(self.text(node), 200) if size <= 200 else li.first_line(r0, r1, 200)
        if size <= cfg.max_bytes:
            out.append(Item(start, r1, kind, scope, sig, name))
        else:
            out.append(self.parts(inner, start, r1, kind, scope, sig, name))

    def unparsed(self, r0: int, r1: int, scope: str, out: list) -> None:
        """解析できなかった範囲。短いもの(1 行のマクロ呼び出しなど)は、ふつうの単位として隣とまとめる。
        長いものだけを、行で分ける代替に回す。"""
        if self.li.size(r0, r1) < self.cfg.min_bytes:
            self.stats.inner_error_units += 1
            out.append(Item(r0, r1, "statement", scope, self.li.first_line(r0, r1, 200)))
        else:
            out.append(Item(r0, r1, "lines", scope, fallback=True))

    def transparent(self, node, start: int, r0: int, r1: int, scope: str, out: list) -> None:
        """スコープにならない入れ物(#if〜#endif、extern "C" { })を、中身を兄弟として並べ直す。"""
        if node.type == "linkage_specification":
            body = node.child_by_field_name("body")
            kids = [] if body is None else (body.named_children if body.type == "declaration_list" else [body])
        else:
            kids = [c for i, c in enumerate(node.children)
                    if c.is_named and node.field_name_for_child(i) not in ("name", "condition")]
        sig = self.li.first_line(r0, r1, 160)
        out.append(Item(start, r0, "preproc", scope, sig))            # `#if …` の行
        self.walk(kids, scope, out)
        last_end = max((self.rows(k)[1] for k in kids), default=r0)
        if r1 > last_end and r1 > r0:
            out.append(Item(r1, r1, "preproc", scope, self.li.first_line(r1, r1, 160)))   # `#endif` や `}` の行

    def tiles(self, node, r0: int, r1: int, out: list[tuple[int, int]]) -> None:
        """node の子ノードの行の範囲を out に足す。大きめの子(上限の 1/3 より大きい)には下りて、細かくする。"""
        li, cfg = self.li, self.cfg
        for k in node.named_children:
            k0, k1 = self.rows(k)
            k0, k1 = max(k0, r0), min(k1, r1)
            if k1 < k0:
                continue
            size = li.size(k0, k1)
            if size > cfg.max_bytes // 3 and k.named_child_count and k1 > k0:
                before = len(out)
                self.tiles(k, k0, k1, out)
                if len(out) == before:
                    out.append((k0, k1))
            elif size > cfg.max_bytes:
                out.extend(split_rows(li, k0, k1, cfg.max_bytes, 10 ** 9))
            else:
                out.append((k0, k1))

    def subdivide(self, node, r0: int, r1: int) -> list[tuple[int, int]]:
        """上限を超える単位を、中の子ノードの切れ目で、同じくらいの大きさに分ける。子が無ければ行で分ける。"""
        li, cfg = self.li, self.cfg
        raw: list[tuple[int, int]] = []
        self.tiles(node, r0, r1, raw)
        if not raw:
            return split_rows(li, r0, r1, cfg.max_bytes, 10 ** 9)
        # 行を分け合う部分はつなぎ、隙間の行(`{` だけの行など)は後ろの部分に入れて、r0〜r1 を隙間なく覆う
        raw.sort()
        tiles: list[list[int]] = []
        for a, b in raw:
            if tiles and a <= tiles[-1][1]:
                tiles[-1][1] = max(tiles[-1][1], b)
            else:
                tiles.append([tiles[-1][1] + 1 if tiles else r0, b])
        tiles[-1][1] = r1
        total = li.size(r0, r1)
        target = total / max(1, -(-total // cfg.max_bytes))      # 部分の数で割った、1 つあたりの目安
        merged: list[list[int]] = []
        for a, b in tiles:
            if merged and li.size(merged[-1][0], b) <= cfg.max_bytes and li.size(merged[-1][0], merged[-1][1]) < target:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        if len(merged) > 1 and li.size(merged[-1][0], merged[-1][1]) < cfg.min_bytes                 and li.size(merged[-2][0], merged[-1][1]) <= cfg.max_bytes + cfg.min_bytes:
            merged[-2][1] = merged[-1][1]                         # 最後に残った短い切れ端は、前に入れる
            merged.pop()
        return [(a, b) for a, b in merged]

    def parts(self, node, r0: int, r1: int, kind: str, scope: str, sig: str, name: str) -> list[Chunk]:
        out: list[Chunk] = []
        limit = self.cfg.max_bytes + self.cfg.min_bytes
        ranges: list[tuple[int, int]] = []
        for a, b in self.subdivide(node, r0, r1):
            if self.li.size(a, b) > limit:     # 長い説明コメントが付いた頭の部分など
                ranges.extend(split_rows(self.li, a, b, self.cfg.max_bytes, 10 ** 9))
            else:
                t = self.li.trim(a, b)
                if t:
                    ranges.append(t)
        for i, (a, b) in enumerate(ranges, 1):
            out.append(Chunk(start_line=a + 1, end_line=b + 1, kind=kind, method="syntax", scope=scope, signature=sig,
                             symbols=[name] if name else [], part=f"{i}/{len(ranges)}" if len(ranges) > 1 else "",
                             text=self.li.text(a, b)))
        return out

    def finish(self, entries: list) -> list[Chunk]:
        out: list[Chunk] = []
        run: list[Item] = []
        for e in entries:
            if isinstance(e, Item):
                run.append(e)
            else:
                if run:
                    out.extend(pack_items(run, self.li, self.cfg, self.stats))
                    run = []
                out.extend(e)
        if run:
            out.extend(pack_items(run, self.li, self.cfg, self.stats))
        return split_oversized(out, self.li, self.cfg)


def chunk_code(text: str, lang: str, cfg: ChunkConfig) -> tuple[list[Chunk], ChunkStats]:
    """C・C++・C# の 1 ファイルを passage に分ける。"""
    li = LineIndex(text)
    stats = ChunkStats(lines=li.nonblank(0, len(li) - 1))
    if not len(li):
        return [], stats
    spec = SPECS[lang]
    try:
        parser, error_query = _load(lang)
        parse_text = sanitize_for_parse(text, cfg) if (cfg.macro_hints and lang in ("c", "cpp")) else text
        src = parse_text.encode("utf-8")
        tree = parser.parse(src)
        from tree_sitter import QueryCursor
        errors = QueryCursor(error_query).captures(tree.root_node).get("e", []) if tree.root_node.has_error else []
    except Exception:   # 解析器が使えない・落ちたときは、ファイル全体を行で分ける
        chunks, stats = chunk_lines(text, cfg)
        stats.parse_failed = True
        stats.fallback_lines = stats.lines
        return chunks, stats
    ctx = _Ctx(li, cfg, spec, src, sorted(n.start_byte for n in errors), stats)
    entries: list = []
    ctx.walk(tree.root_node.named_children, "", entries)
    chunks = cover_gaps(merge_tiny(ctx.finish(entries), li, cfg), li, cfg, stats)
    return chunks, stats
