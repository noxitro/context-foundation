"""分割: 構文の単位・スコープとシグネチャ・長い単位と短い単位・解析できない範囲の代替・Markdown。"""
from __future__ import annotations

import pytest

from context_foundation.chunking import chunk_file
from context_foundation.config import ChunkConfig
from context_foundation.ingest import decode_bytes, language_of, normalize_newlines

from conftest import CORPUS

CFG = ChunkConfig()


def load(rel: str) -> tuple[str, str]:
    text, _ = decode_bytes((CORPUS / rel).read_bytes())
    return normalize_newlines(text), language_of(rel) or "text"


def chunks_of(rel: str, cfg: ChunkConfig = CFG):
    text, lang = load(rel)
    chunks, stats = chunk_file(text, lang, cfg)
    return text, chunks, stats


ALL_FILES = sorted(p.relative_to(CORPUS).as_posix() for p in CORPUS.rglob("*") if p.is_file())


@pytest.mark.parametrize("rel", ALL_FILES)
def test_passages_are_line_ranges_of_the_file(rel):
    """どのファイルでも: 順に並び、重ならず、本文は元の行そのまま、空でない行を 1 つも落とさない。"""
    text, chunks, _ = chunks_of(rel)
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    covered: set[int] = set()
    prev_end = 0
    assert chunks
    for c in chunks:
        assert 1 <= c.start_line <= c.end_line <= len(lines)
        assert c.start_line > prev_end
        assert c.text == "\n".join(lines[c.start_line - 1:c.end_line])
        covered.update(range(c.start_line, c.end_line + 1))
        prev_end = c.end_line
    missing = [n for n, line in enumerate(lines, 1) if line.strip() and n not in covered]
    assert not missing


def test_cpp_functions_carry_scope_and_signature():
    _, chunks, stats = chunks_of("engine/Render/BufferPool.cpp")
    by_name = {s: c for c in chunks for s in c.symbols}
    acquire = by_name["Acquire"]
    assert acquire.kind == "function" and acquire.method == "syntax"
    assert acquire.scope == "kes::render::BufferPool"
    assert acquire.signature == "Buffer* BufferPool::Acquire(const BufferDesc& desc)"
    assert acquire.start_line == 28
    reusable = by_name["FindReusable"]
    assert (reusable.start_line, reusable.end_line) == (53, 69)
    assert stats.fallback_lines == 0


def test_cpp_export_macro_does_not_break_the_class():
    """`class KES_API BufferPool` の KES_API は、解析の前に伏せる。本文は元のまま。"""
    _, chunks, stats = chunks_of("engine/Render/BufferPool.h")
    cls = next(c for c in chunks if "BufferPool" in c.symbols and c.kind == "class")
    assert cls.signature == "class BufferPool final"
    assert "class KES_API BufferPool final" in cls.text
    assert "Acquire" in cls.symbols
    assert "//! Hands out GPU buffers" in cls.text          # 直前の説明コメントは、クラスに含める
    assert stats.fallback_lines == 0


def test_cpp_long_function_is_split_with_the_same_signature():
    body = "\n".join(f"    total += Compute({i}, values[{i}]) * weights[{i}];" for i in range(120))
    text = f"namespace demo {{\nint Sum(const int* values, const int* weights)\n{{\n    int total = 0;\n{body}\n    return total;\n}}\n}}\n"
    chunks, _ = chunk_file(text, "cpp", CFG)
    parts = [c for c in chunks if c.symbols == ["Sum"]]
    assert len(parts) >= 3
    assert all(c.signature == "int Sum(const int* values, const int* weights)" for c in parts)
    assert all(c.scope == "demo" and c.kind == "function" for c in parts)
    assert [c.part for c in parts] == [f"{i}/{len(parts)}" for i in range(1, len(parts) + 1)]
    assert all(len(c.text.encode()) <= CFG.max_bytes + CFG.min_bytes for c in parts)


def test_cpp_small_units_are_merged_within_a_scope():
    text = "namespace demo {\n" + "".join(f"int Get{i}() {{ return {i}; }}\n" for i in range(6)) + "}\n"
    chunks, _ = chunk_file(text, "cpp", CFG)
    assert len(chunks) == 1
    assert {f"Get{i}" for i in range(6)} <= set(chunks[0].symbols)


def test_cpp_unparsable_region_falls_back_to_lines():
    """マクロで文法が崩れた範囲は、行で分け、その行数を数える。前後の関数は構文の単位のまま。"""
    entries = "\n".join(f"    (Entry{i}, {i}, \"entry number {i} of the table\")," for i in range(40))
    text = ("namespace demo {\nint Before() { return 1; }\n\n"
            f"DEMO_TABLE_WITH_NAMES(Kind, unsigned,\n{entries}\n    (Count, 40))\n\n"
            "int After(int value)\n{\n    return value + 1;\n}\n}\n")
    chunks, stats = chunk_file(text, "cpp", CFG)
    assert stats.fallback_lines >= 40
    fallback = [c for c in chunks if c.method == "lines"]
    assert fallback and all(c.kind == "lines" and c.scope == "demo" for c in fallback)
    after = next(c for c in chunks if "After" in c.symbols)
    assert after.method == "syntax" and after.signature == "int After(int value)"


def test_error_in_a_class_header_does_not_lose_the_members():
    """継承の並びの途中に #if があると、見出しの行は解析できない。本体は読めているので、メンバーは構文の単位のまま。"""
    methods = "\n".join(
        f"    /// <summary>Does thing {i}.</summary>\n    public int Method{i}(int value)\n    {{\n"
        f"        return Compute(value, {i}) + Offset(value, {i}) * Scale(value, {i});\n    }}\n" for i in range(30))
    text = ("namespace Demo.Linq\n{\n    public abstract class Token : IEnumerable<Token>\n#if HAVE_CLONE\n"
            "        , ICloneable\n#endif\n    {\n" + methods + "    }\n}\n")
    chunks, stats = chunk_file(text, "csharp", CFG)
    assert stats.fallback_lines == 0 and stats.inner_error_units >= 1
    later = next(c for c in chunks if "Method25" in c.symbols)     # 小さなメソッドは、隣どうしでまとまる
    assert later.kind == "method" and later.method == "syntax" and later.scope == "Demo.Linq.Token"
    head = next(c for c in chunks if c.kind == "class")
    assert head.signature == "public abstract class Token : IEnumerable<Token>"
    assert head.scope == "Demo.Linq.Token" and "Method0" in head.symbols
    assert len(chunks) >= 4 and all(c.method == "syntax" for c in chunks)


def test_cpp_test_macro_with_body_stays_a_function():
    text = "TEST_F(PoolTests, ReusesBuffers)\n{\n    EXPECT_TRUE(Run());\n}\n"
    chunks, stats = chunk_file(text, "cpp", CFG)
    assert chunks[0].kind == "function" and chunks[0].signature == "TEST_F(PoolTests, ReusesBuffers)"
    assert stats.fallback_lines == 0


def test_csharp_scope_and_signature():
    _, chunks, _ = chunks_of("app/Save/SaveGameService.cs")
    save = next(c for c in chunks if "SaveAsync" in c.symbols)
    assert save.kind == "method"
    assert save.scope == "Kestrel.App.Save.SaveGameService"      # file-scoped namespace + クラス
    assert save.signature == "public async Task SaveAsync(string slot, byte[] payload)"
    assert save.text.lstrip().startswith("/// <summary>")
    _, chunks, _ = chunks_of("app/Input/InputRebinder.cs")
    rebind = next(c for c in chunks if "Rebind" in c.symbols)
    assert rebind.scope == "Kestrel.App.Input.InputRebinder"     # 波かっこの namespace + クラス


def test_markdown_headings_become_scope():
    _, chunks, _ = chunks_of("docs/rendering.md")
    reuse = next(c for c in chunks if "Reuse delay" in c.symbols)
    assert reuse.kind == "heading" and reuse.method == "heading"
    assert reuse.scope == "Rendering design notes > Frame resources"
    assert reuse.signature == "Reuse delay"


def test_markdown_ignores_headings_in_code_and_front_matter():
    text = ("---\ntitle: demo\n---\n\n# Title\n\n" + "Intro text. " * 40 + "\n\n```sh\n# not a heading\necho hi\n```\n\n"
            "## Real section\n\n" + "Body text. " * 60 + "\n")
    chunks, _ = chunk_file(text, "markdown", CFG)
    titles = [s for c in chunks for s in c.symbols]
    assert "not a heading" not in titles and "Real section" in titles
    assert next(c for c in chunks if "Real section" in c.symbols).scope == "Title"
    assert chunks[0].signature == "title: demo" and chunks[0].kind == "text"      # front matter は見出しにしない
    assert "# not a heading" in next(c for c in chunks if "Title" in c.symbols).text


def test_markdown_short_sections_merge_with_their_parent():
    text = "# Title\n\nIntro.\n\n## Small\n\nShort body.\n\n## Other\n\nAlso short.\n"
    chunks, _ = chunk_file(text, "markdown", CFG)
    assert len(chunks) == 1 and chunks[0].symbols == ["Title", "Small", "Other"]


def test_japanese_text_is_sized_in_bytes():
    """日本語は 1 文字を 3 と数える(埋め込みモデルが読める量に収めるため)。"""
    _, chunks, _ = chunks_of("docs/save-data.md")
    assert len(chunks) >= 3
    assert all(len(c.text.encode("utf-8")) <= CFG.max_bytes + CFG.min_bytes for c in chunks)
    section = next(c for c in chunks if "書き込みの手順" in c.symbols)
    assert section.scope == "セーブデータの設計"


def test_other_files_are_split_by_lines():
    text = "\n".join(f"setting_{i} = value number {i}" for i in range(200)) + "\n"
    chunks, stats = chunk_file(text, "text", CFG)
    assert len(chunks) > 1 and all(c.method == "lines" for c in chunks)
    assert all(c.end_line - c.start_line + 1 <= CFG.line_window for c in chunks)
    assert stats.fallback_lines == 0     # 行で分けるのが元々の方法なので、代替には数えない
