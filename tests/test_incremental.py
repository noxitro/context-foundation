"""差分更新: 前の索引の埋め込みを、変わっていない passage の分だけ使い回す。

確かめること:
- 変えたファイル・足したファイルの passage だけを埋め込み直し、消したファイルの passage は残らない
- 差分更新の結果(passage と埋め込み)は、同じコーパスを作り直したときと同じ
- 埋め込みの作り方が変わったとき・前の索引に埋め込みが無いときは、すべて埋め込み直す
- 変更の検知に git を使わない(フィクスチャの写しは git の外)
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import numpy as np

from context_foundation.cli import main as cli_main
from context_foundation.config import load_config
from context_foundation.indexer import build_index
from context_foundation.search import Searcher
from context_foundation.store import load_vectors, open_readonly, read_meta, unpack_text

from conftest import write_config


def _build(ws: Path, corpus: Path, extra: str = "", **kw) -> dict:
    return build_index(load_config(write_config(ws, corpus, extra)), progress=False, log=lambda s: None, **kw)


def _snapshot(ws: Path) -> tuple[list[tuple], np.ndarray]:
    """索引の passage(場所と本文)と、id の順の埋め込み。"""
    with closing(open_readonly(ws / ".cf" / "index.db")) as db:
        meta = read_meta(db)
        rows = [(r[0], r[1], r[2], r[3], unpack_text(r[4])) for r in db.execute(
            "SELECT source_id, path, start_line, end_line, text FROM chunks ORDER BY id")]
        emb = meta["embedding"]
        vecs = load_vectors(db, emb["dim"], emb["vector_dtype"])
    return rows, vecs


def _edit_corpus(corpus: Path) -> None:
    # 1 つ書き換える・1 つ足す・1 つ消す
    p = corpus / "engine" / "Core" / "JobScheduler.cpp"
    p.write_text(p.read_text(encoding="utf-8").replace("Pick a random victim", "Pick a victim at random"),
                 encoding="utf-8")
    (corpus / "docs" / "added-note.md").write_text(
        "# Added note\n\nThe streaming system loads texture mips progressively.\n", encoding="utf-8")
    (corpus / "app" / "Input" / next(iter(sorted(x.name for x in (corpus / "app" / "Input").iterdir())))).unlink()


def test_incremental_embeds_only_changed_passages(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    first = _build(ws, corpus_copy)
    total0 = first["stats"]["chunks"]
    assert first["timings"]["embedded_passages"] == total0 and first["timings"]["reused_passages"] == 0

    # 何も変えずに作り直すと、すべて使い回す
    again = _build(ws, corpus_copy)
    assert again["timings"]["reused_passages"] == total0 and again["timings"]["embedded_passages"] == 0

    _edit_corpus(corpus_copy)
    after = _build(ws, corpus_copy)
    t = after["timings"]
    assert t["embedded_passages"] >= 2                       # 書き換えた passage と、足したファイルの passage
    assert t["embedded_passages"] < after["stats"]["chunks"] // 2
    assert t["reused_passages"] + t["embedded_passages"] == after["stats"]["chunks"]
    rows, _ = _snapshot(ws)
    paths = {(r[0], r[1]) for r in rows}
    assert ("docs", "added-note.md") in paths
    assert not any(r[0] == "app" and r[1].startswith("Input/") for r in rows)   # 消したファイルは残らない


def test_incremental_equals_full_rebuild(tmp_path, corpus_copy):
    inc, full = tmp_path / "inc", tmp_path / "full"
    _build(inc, corpus_copy)
    _edit_corpus(corpus_copy)
    _build(inc, corpus_copy)                                  # 差分更新
    _build(full, corpus_copy, reuse=False)                   # 同じコーパスを作り直す
    rows_i, vecs_i = _snapshot(inc)
    rows_f, vecs_f = _snapshot(full)
    assert rows_i == rows_f
    assert vecs_i.shape == vecs_f.shape and np.array_equal(vecs_i, vecs_f)
    q = "worker steals a job from another queue"
    for mode in ("keyword", "semantic", "hybrid"):
        a = Searcher.from_config(load_config(inc / ".cf" / "config.toml")).search(q, mode=mode, top_k=5)
        b = Searcher.from_config(load_config(full / ".cf" / "config.toml")).search(q, mode=mode, top_k=5)
        assert [(p["path"], p["start_line"]) for p in a.passages] == [(p["path"], p["start_line"]) for p in b.passages]


def test_changed_embedding_settings_reembed_everything(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    # 保存の形が変われば、使い回さない
    r = _build(ws, corpus_copy, extra="[embedding]\nvector_dtype = 'float32'\n")
    assert r["timings"]["reused_passages"] == 0 and r["timings"]["embedded_passages"] == r["stats"]["chunks"]


def test_keyword_only_previous_index_is_not_reused(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy, use_embedding=False)
    r = _build(ws, corpus_copy)
    assert r["timings"]["reused_passages"] == 0 and r["embedding"]


def test_cli_full_flag_reembeds_everything(tmp_path, corpus_copy, capsys, monkeypatch):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    monkeypatch.chdir(ws)
    assert cli_main(["index", "--quiet", "--json", "--full"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["timings"]["reused_passages"] == 0 and out["timings"]["embedded_passages"] == out["stats"]["chunks"]
    assert cli_main(["index", "--quiet", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["timings"]["embedded_passages"] == 0
