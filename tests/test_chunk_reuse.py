"""差分更新で、中身の変わらないファイルの passage を、分け直さずに前の索引から写す(0.4.5 から)。

確かめること:
- 写した索引は、同じコーパスを作り直した索引と同じ(passage・keyword の索引の語と位置・埋め込み・統計・検索の順位)
- 写すのは、source・パス・言語・本文が同じファイルだけ。大きさと更新日時が同じでも、本文が違えば分け直す
- 分割の作り方(設定・分け方のコード・道具の版)が変わったとき、--full、構文で分けられなかったファイル、
  控えの無い前の索引(0.4.4 まで)では、写さない
- 識別子の語の分け方を覚えても、出す語は変わらない
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from context_foundation import textproc
from context_foundation.config import load_config
from context_foundation.indexer import build_index
from context_foundation.search import Searcher

from conftest import CORPUS, write_config

VOLATILE_META = ("build_id", "created_at", "timings")


def _build(ws: Path, corpus: Path, extra: str = "", log=None, **kw) -> dict:
    return build_index(load_config(write_config(ws, corpus, extra)), progress=False, log=log or (lambda s: None), **kw)


def _dump(ws: Path) -> dict:
    """索引の中身のうち、作るたびに変わるもの(作った日時・印・時間)を除いた全部。"""
    with closing(sqlite3.connect(str(ws / ".cf" / "index.db"))) as db:
        out = {
            "files": db.execute("SELECT id, source_id, path, size, lines, language, encoding, chunks, fallback_lines "
                                "FROM files ORDER BY id").fetchall(),
            "chunks": db.execute("SELECT * FROM chunks ORDER BY id").fetchall(),
            "file_chunking": db.execute("SELECT * FROM file_chunking ORDER BY file_id").fetchall(),
            "vectors": db.execute("SELECT * FROM vectors ORDER BY block").fetchall(),
        }
        # keyword の索引は本文を持たない(contentless)ので、語ごとの数と、語の出た位置を比べる
        db.execute("CREATE VIRTUAL TABLE temp.vrow USING fts5vocab(main, 'chunks_fts', 'row')")
        db.execute("CREATE VIRTUAL TABLE temp.vinst USING fts5vocab(main, 'chunks_fts', 'instance')")
        out["fts_row"] = db.execute("SELECT * FROM temp.vrow ORDER BY term").fetchall()
        out["fts_instance"] = db.execute("SELECT * FROM temp.vinst ORDER BY term, doc, col, offset").fetchall()
        meta = dict(db.execute("SELECT key, value FROM meta"))
    for k in VOLATILE_META:
        meta.pop(k, None)
    out["meta"] = meta
    return out


def _edit(corpus: Path) -> None:
    # 書き換える・足す・消す・更新日時だけ変える
    p = corpus / "engine" / "Core" / "JobScheduler.cpp"
    p.write_text(p.read_text(encoding="utf-8").replace("Pick a random victim", "Pick a victim at random"),
                 encoding="utf-8")
    (corpus / "docs" / "added-note.md").write_text(
        "# Added note\n\nThe streaming system loads texture mips progressively.\n", encoding="utf-8")
    (corpus / "app" / "Input" / sorted(x.name for x in (corpus / "app" / "Input").iterdir())[0]).unlink()
    touched = corpus / "engine" / "Render" / "BufferPool.h"
    st = touched.stat()
    os.utime(touched, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_unchanged_files_are_copied_not_rechunked(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    first = _build(ws, corpus_copy)
    t = first["timings"]
    assert t["reused_chunk_files"] == 0 and t["chunked_files"] == first["stats"]["files"]
    logs: list[str] = []
    again = _build(ws, corpus_copy, log=logs.append)
    t = again["timings"]
    assert t["reused_chunk_files"] == again["stats"]["files"] and t["chunked_files"] == 0
    assert any("前の索引の passage を写しました" in s for s in logs)

    _edit(corpus_copy)
    after = _build(ws, corpus_copy)
    t = after["timings"]
    assert t["chunked_files"] == 2              # 書き換えたファイルと、足したファイル(更新日時だけ変えたものは写す)
    assert t["reused_chunk_files"] == after["stats"]["files"] - 2


@pytest.mark.parametrize("embed", [True, False])
def test_copied_index_equals_full_rebuild(tmp_path, corpus_copy, embed):
    inc, full = tmp_path / "inc", tmp_path / "full"
    _build(inc, corpus_copy, use_embedding=embed)
    _edit(corpus_copy)
    r = _build(inc, corpus_copy, use_embedding=embed)                  # 差分更新(分割を写す)
    assert r["timings"]["reused_chunk_files"] > 0
    f = _build(full, corpus_copy, use_embedding=embed, reuse=False)    # 同じコーパスを作り直す
    assert f["timings"]["reused_chunk_files"] == 0
    a, b = _dump(inc), _dump(full)
    for key in a:
        assert a[key] == b[key], key
    assert r["stats"] == f["stats"]
    for q in ("worker steals a job from another queue", "バッファの再利用", "BufferPool Acquire"):
        for mode in (("keyword", "semantic", "hybrid") if embed else ("keyword",)):
            x = Searcher.from_config(load_config(inc / ".cf" / "config.toml")).search(q, mode=mode, top_k=10)
            y = Searcher.from_config(load_config(full / ".cf" / "config.toml")).search(q, mode=mode, top_k=10)
            assert [(p["path"], p["start_line"], p["score"]) for p in x.passages] == \
                   [(p["path"], p["start_line"], p["score"]) for p in y.passages]


def test_same_size_and_mtime_but_different_text_is_rechunked(tmp_path, corpus_copy):
    """更新日時ではなく、本文で見分ける(大きさも更新日時も同じまま中身が変わっても、取りこぼさない)。"""
    ws, full = tmp_path / "ws", tmp_path / "full"
    _build(ws, corpus_copy)
    p = corpus_copy / "engine" / "Core" / "JobScheduler.cpp"
    st = p.stat()
    data = p.read_bytes()
    assert b"victim" in data
    # バイトのまま書く(write_text は Windows で改行を CRLF にして、大きさが変わる)
    p.write_bytes(data.replace(b"victim", b"VICTIM"))                      # 同じ長さ
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert p.stat().st_size == st.st_size and p.stat().st_mtime_ns == st.st_mtime_ns
    r = _build(ws, corpus_copy)
    assert r["timings"]["chunked_files"] == 1
    _build(full, corpus_copy, reuse=False)
    assert _dump(ws)["chunks"] == _dump(full)["chunks"]


@pytest.mark.parametrize("extra", ["[chunk]\nmax_bytes = 900\n", "[chunk]\nmacro_hints = false\n",
                                   "[chunk]\nattribute_macros = ['MY_API']\n"])
def test_changed_chunk_settings_rechunk_everything(tmp_path, corpus_copy, extra):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    r = _build(ws, corpus_copy, extra=extra)
    assert r["timings"]["reused_chunk_files"] == 0
    # その設定のまま、もう一度作れば写す
    assert _build(ws, corpus_copy, extra=extra)["timings"]["chunked_files"] == 0


@pytest.mark.parametrize("key", ["tool_version", "code", "parsers", "schema_version"])
def test_changed_chunking_signature_rechunks_everything(tmp_path, corpus_copy, monkeypatch, key):
    """道具の版・分け方のコード・構文の解析器の版・索引の形式の版のどれかが違えば、写さない。"""
    from context_foundation import indexer
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    real = indexer.chunking_signature
    monkeypatch.setattr(indexer, "chunking_signature", lambda cfg: {**real(cfg), key: "changed"})
    assert _build(ws, corpus_copy)["timings"]["reused_chunk_files"] == 0


def test_chunking_signature_follows_the_chunking_code(tmp_path, monkeypatch):
    """分け方のコードのハッシュは、chunking の .py の中身から取る。"""
    from context_foundation import indexer
    cfg = load_config(write_config(tmp_path, CORPUS))
    sig = indexer.chunking_signature(cfg)
    assert len(sig["code"]) == 64 and set(sig["parsers"]) >= {"tree-sitter", "tree-sitter-cpp"}
    fake = tmp_path / "pkg"
    (fake / "chunking").mkdir(parents=True)
    for p in (Path(indexer.__file__).parent / "chunking").glob("*.py"):
        (fake / "chunking" / p.name).write_bytes(p.read_bytes())
    monkeypatch.setattr(indexer, "__file__", str(fake / "indexer.py"))
    assert indexer.chunking_signature(cfg)["code"] == sig["code"]
    (fake / "chunking" / "code.py").write_bytes((fake / "chunking" / "code.py").read_bytes() + b"\n# changed\n")
    assert indexer.chunking_signature(cfg)["code"] != sig["code"]


def test_full_flag_rechunks_everything(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    r = _build(ws, corpus_copy, reuse=False)
    assert r["timings"]["reused_chunk_files"] == 0


def test_file_that_fell_back_to_lines_is_rechunked(tmp_path, corpus_copy, monkeypatch):
    """構文で分けられなかったファイルは、控えを残さない(次も分け直し、作り直したときと同じ注意を出す)。"""
    from context_foundation import indexer
    ws = tmp_path / "ws"
    real = indexer.chunk_file

    def failing(text, language, cfg):
        if "Pick a random victim" in text:
            raise RecursionError("too deep")
        return real(text, language, cfg)

    monkeypatch.setattr(indexer, "chunk_file", failing)
    first = _build(ws, corpus_copy)
    logs: list[str] = []
    second = _build(ws, corpus_copy, log=logs.append)
    assert second["timings"]["chunked_files"] == 1
    assert any("構文で分けられなかったので" in s for s in logs)
    assert second["stats"] == first["stats"]


def test_previous_index_without_chunking_record_still_builds(tmp_path, corpus_copy):
    """0.4.4 までの索引(分割の控えが無い)の上でも作れる。そのときは、すべて分け直す。"""
    ws, full = tmp_path / "ws", tmp_path / "full"
    _build(ws, corpus_copy)
    with closing(sqlite3.connect(str(ws / ".cf" / "index.db"))) as db:
        db.execute("DROP TABLE file_chunking")
        db.execute("DELETE FROM meta WHERE key = 'chunking'")
        db.commit()
    r = _build(ws, corpus_copy)
    assert r["timings"]["reused_chunk_files"] == 0 and r["timings"]["reused_passages"] > 0   # 埋め込みは使い回す
    assert _build(ws, corpus_copy)["timings"]["chunked_files"] == 0
    _build(full, corpus_copy, reuse=False)
    assert _dump(ws) == _dump(full)


def test_damaged_chunking_record_is_not_copied(tmp_path, corpus_copy):
    """控えと passage の数が合わないファイルは、写さずに分け直す。"""
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    with closing(sqlite3.connect(str(ws / ".cf" / "index.db"))) as db:
        fid = db.execute("SELECT id FROM files WHERE chunks >= 2 ORDER BY id LIMIT 1").fetchone()[0]
        db.execute("UPDATE files SET chunks = chunks + 1 WHERE id = ?", (fid,))
        db.commit()
    assert _build(ws, corpus_copy)["timings"]["chunked_files"] == 1


def test_identifier_cache_gives_the_same_tokens():
    texts = [p.read_text(encoding="utf-8") for p in sorted(CORPUS.rglob("*")) if p.is_file()
             and p.suffix in (".cpp", ".h", ".cs", ".md")]
    texts += ["GetBufferPool get_buffer_pool HTTPServer2Config __init__ x86_64 " + "A" * 100]
    textproc._identifier_tokens_lru.cache_clear()
    uncached = [[t for m in textproc._RUN.finditer(s) for t in (
        textproc._cjk_tokens(m.group(0)) if textproc._CJK.match(m.group(0))
        else textproc._identifier_tokens(m.group(0)) if m.group(0)[0].isascii()
        else [m.group(0).lower()[:textproc.MAX_TOKEN_LEN]])] for s in texts]
    assert [textproc.tokenize(s) for s in texts] == uncached
    assert [textproc.tokenize(s) for s in texts] == uncached        # 覚えた後も同じ
    info = textproc._identifier_tokens_lru.cache_info()
    assert info.hits > 0 and info.maxsize == textproc.IDENTIFIER_CACHE_SIZE
    # 長い連なりは覚えない(出す語は同じ)
    long_run = "Ab" * textproc.IDENTIFIER_CACHE_MAX_LEN
    before = textproc._identifier_tokens_lru.cache_info().currsize
    assert textproc.tokenize(long_run) == textproc._identifier_tokens(long_run)
    assert textproc._identifier_tokens_lru.cache_info().currsize == before
    # 覚えた結果を書き換えられない(呼び出し側が list を変えても、次の結果に響かない)
    out = textproc.tokenize("GetBufferPool")
    out.append("x")
    assert textproc.tokenize("GetBufferPool") == ["getbufferpool", "get", "buffer", "pool"]
    assert np.all(np.array([len(t) <= textproc.MAX_TOKEN_LEN for s in texts for t in textproc.tokenize(s)]))
