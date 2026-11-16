"""中身の無い passage(ライセンスのコメントと include だけのファイルの頭など)は、埋め込まない。

確かめること:
- 判定(has_content): 前処理の行・using・名前空間の開き閉じ・前方宣言・括弧だけなら、中身が無い
- 埋め込まなかった passage は、意味検索には出ず、keyword では引ける
- 差分更新で、埋め込まなかった passage を「使い回した」ことにしない
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from context_foundation import indexer
from context_foundation.config import load_config
from context_foundation.indexer import build_index, has_content
from context_foundation.search import Searcher
from context_foundation.store import load_vectors, open_readonly, read_meta

LICENSE = "/*\n * This file is part of the example engine.\n * SPDX-License-Identifier: MIT\n */\n"

HEADER = LICENSE + """#pragma once

#include <render/Buffer.h>
#include <render/BufferView.h>
#include <render/Device.h>
#include <render/DeviceObject.h>
#include <render/MemoryAllocation.h>
#include <render/ResourcePool.h>
#include <render/ResourcePoolDescriptor.h>
#include <render/StagingMemoryAllocator.h>
#include <core/containers/vector.h>
#include <core/memory/intrusive_ptr.h>

namespace render
{
    //! Hands out buffers from one large allocation and recycles them when they are released,
    //! so that creating a buffer every frame does not reach the driver each time.
    class BufferPool
    {
    public:
        //! Reserves the backing allocation for the pool.
        bool Init(Device& device, const ResourcePoolDescriptor& descriptor);

        //! Gives the buffer a region of the pool. Fails when the pool has no room left.
        bool InitBuffer(Buffer& buffer, unsigned long long byteCount);

        //! Returns the region of the buffer to the pool, to be handed out again.
        void ShutdownBuffer(Buffer& buffer);

    private:
        intrusive_ptr<MemoryAllocation> m_allocation;
        vector<Buffer*> m_buffers;
    };
}
"""


@pytest.mark.parametrize("text, language, start_line, expected", [
    (LICENSE + "#pragma once\n\n#include <a/b.h>\n#include \"c.h\"  // for x\n", "cpp", 1, False),
    (LICENSE, "cpp", 1, False),                                   # ライセンスのコメントだけ
    ("}   // namespace render", "cpp", 96, False),
    ("    };\n}", "cpp", 50, False),
    ("namespace a\n{\n    namespace b\n    {\n        class Buffer;\n        struct Foo;\n        enum class Bar;",
     "cpp", 12, False),
    ("using namespace render;\nusing System.Text;\n#endif", "csharp", 3, False),
    ("", "cpp", 5, False),
    ("#ifndef POOL_H\n#define POOL_H\n\n#include <a.h>\n#endif // POOL_H", "cpp", 9, False),    # include ガード
    ("namespace Foo.Bar;\nusing static System.Math;\nglobal using System;\n#nullable enable", "csharp", 1, False),
    ("namespace a { class X; }\nnamespace a { class X; struct Y; }", "cpp", 20, False),
    ("class RENDER_API Buffer;\nstruct RENDER_API View;\nenum class Kind;", "cpp", 20, False),
    ("// Explains how the pool recycles buffers.", "cpp", 30, True),     # コメントは中身
    ("#define MAX_POOL 4", "cpp", 30, True),
    ("#define MAX(a, b) ((a) > (b) ? (a) : (b))", "cpp", 30, True),
    ("class Foo {};", "cpp", 30, True),
    ("namespace a { int x; }", "cpp", 30, True),
    ("enum class Kind : int;", "cpp", 30, True),                    # 型を決めている宣言は中身
    ("using BufferPtr = Ptr<Buffer>;", "cpp", 30, True),
    ("int Count() const;", "cpp", 30, True),
    (LICENSE + "int x;", "cpp", 1, True),
    ("#include <a.h>", "text", 1, True),                          # 構文で分けない言語は、判定しない
    ("# include notes", "markdown", 1, True),
])
def test_has_content(text, language, start_line, expected):
    assert has_content(text, language, start_line) is expected


def _workspace(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "render").mkdir(parents=True)
    (corpus / "render" / "BufferPool.h").write_text(HEADER, encoding="utf-8", newline="\n")
    ws = tmp_path / "ws"
    (ws / ".cf").mkdir(parents=True)
    (ws / ".cf" / "config.toml").write_text(f"[[source]]\nid = 'engine'\nroot = '{corpus}'\n", encoding="utf-8")
    return ws


def _build(ws: Path, **kw) -> dict:
    return build_index(load_config(ws / ".cf" / "config.toml"), progress=False, log=lambda s: None, **kw)


def _rows(ws: Path) -> list[tuple[int, int, bool]]:
    """(始まりの行, 終わりの行, 埋め込みがあるか) を、id の順に。"""
    with closing(open_readonly(ws / ".cf" / "index.db")) as db:
        emb = read_meta(db)["embedding"]
        vecs = load_vectors(db, emb["dim"], emb["vector_dtype"])
        return [(r[1], r[2], bool(vecs[r[0] - 1].any()))
                for r in db.execute("SELECT id, start_line, end_line FROM chunks ORDER BY id")]


def test_passage_without_content_is_not_embedded(tmp_path):
    ws = _workspace(tmp_path)
    out = _build(ws)
    rows = _rows(ws)
    head = [r for r in rows if r[0] == 1]
    assert len(head) == 1 and not head[0][2]                     # ファイルの頭(ライセンス + include)は埋め込まない
    assert any(r[2] for r in rows)                               # クラスの passage は埋め込む
    t = out["timings"]
    assert t["unembedded_passages"] == sum(1 for r in rows if not r[2]) >= 1
    assert t["embedded_passages"] + t["unembedded_passages"] == out["stats"]["chunks"]

    s = Searcher.from_config(load_config(ws / ".cf" / "config.toml"))
    sem = s.search("render StagingMemoryAllocator ResourcePoolDescriptor include", mode="semantic", top_k=50)
    assert sem.passages and all(p["start_line"] != 1 for p in sem.passages)      # 意味検索には出ない
    kw = s.search("StagingMemoryAllocator", mode="keyword", top_k=5)
    assert kw.passages[0]["start_line"] == 1                                       # keyword では引ける
    hy = s.search("StagingMemoryAllocator", mode="hybrid", top_k=5)
    head = [p for p in hy.passages if p["start_line"] == 1]
    assert len(head) == 1 and head[0]["ranks"] == {"keyword": 1, "semantic": None}


def _everything_has_content(text, language, start_line=0):
    return True


def test_index_built_before_this_rule_switches_without_reembedding(tmp_path, monkeypatch):
    """すべての passage を埋め込んでいた版の索引から、埋め込み直しなしで切り替わる。"""
    ws = _workspace(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(indexer, "has_content", _everything_has_content)      # 前の版と同じ: すべて埋め込む
        old = _build(ws)
    total = old["stats"]["chunks"]
    assert old["timings"]["embedded_passages"] == total and all(r[2] for r in _rows(ws))

    new = _build(ws)
    rows = _rows(ws)
    skipped = sum(1 for r in rows if not r[2])
    assert skipped >= 1 and not [r for r in rows if r[0] == 1][0][2]
    t = new["timings"]
    assert (t["embedded_passages"], t["reused_passages"], t["unembedded_passages"]) == (0, total - skipped, skipped)


def test_source_filter_keeps_unembedded_passages_out(tmp_path):
    ws = _workspace(tmp_path)
    _build(ws)
    s = Searcher.from_config(load_config(ws / ".cf" / "config.toml"))
    resp = s.search("render StagingMemoryAllocator ResourcePoolDescriptor include", mode="semantic", top_k=50,
                    source_ids=["engine"])
    assert resp.passages and all(p["start_line"] != 1 for p in resp.passages)


def test_long_lived_searcher_follows_a_replaced_index(tmp_path, monkeypatch):
    """作ったままの Searcher(MCP サーバー)は、索引が置き換わったら、埋め込みの無い passage の位置も読み直す。"""
    ws = _workspace(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(indexer, "has_content", _everything_has_content)
        _build(ws)
    s = Searcher.from_config(load_config(ws / ".cf" / "config.toml"))
    query = "render StagingMemoryAllocator ResourcePoolDescriptor include"
    assert any(p["start_line"] == 1 for p in s.search(query, mode="semantic", top_k=50).passages)

    _build(ws)                                                   # 今の決まりで作り直す(ファイルが置き換わる)
    after = s.search(query, mode="semantic", top_k=50).passages
    assert after and all(p["start_line"] != 1 for p in after)


def test_status_reports_unembedded_passages(tmp_path):
    from context_foundation.render import status_human
    ws = _workspace(tmp_path)
    out = _build(ws)
    s = Searcher.from_config(load_config(ws / ".cf" / "config.toml"))
    assert f"埋め込んでいない passage: {out['timings']['unembedded_passages']:,}" in status_human(s.status())


def test_incremental_update_does_not_reuse_missing_embeddings(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    first = _build(ws)
    skipped = first["timings"]["unembedded_passages"]
    total = first["stats"]["chunks"]
    assert skipped >= 1

    again = _build(ws)                                           # 何も変えずに作り直す
    assert again["timings"]["embedded_passages"] == 0
    assert again["timings"]["reused_passages"] == total - skipped
    assert again["timings"]["unembedded_passages"] == skipped

    # 判定が変わって、前は埋め込まなかった passage を埋め込むことになっても、0 のベクトルを使い回さない
    monkeypatch.setattr(indexer, "has_content", lambda text, language, start_line=0: True)
    later = _build(ws)
    assert later["timings"]["embedded_passages"] == skipped
    assert later["timings"]["reused_passages"] == total - skipped
    assert later["timings"]["unembedded_passages"] == 0
    assert all(r[2] for r in _rows(ws))
