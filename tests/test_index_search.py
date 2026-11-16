"""索引と検索: keyword・semantic・hybrid、source の絞り込み、モデルが無いときの切り替え、出力の上限。

埋め込みは、テスト用の HashEmbedder(語のハッシュ。意味は見ない)なので、ここで確かめるのは
仕組みが通しで動くことと、keyword の一致のしかた。意味検索の質は、モデルの実物で別に確かめる。
"""
from __future__ import annotations

import os
import time

import pytest

from context_foundation.config import load_config
from context_foundation.indexer import build_index, embedding_text, strip_license_header
from context_foundation.render import HARD_MAX_BYTES, status_agent, status_human, to_agent, to_human
from context_foundation.search import Searcher, rrf
from context_foundation.store import IndexError_

from conftest import write_config


@pytest.fixture(scope="module")
def searcher(indexed_workspace):
    return Searcher.from_config(load_config(indexed_workspace / ".cf" / "config.toml"))


def locs(resp):
    return [(p["source_id"], p["path"], p["start_line"], p["end_line"]) for p in resp.passages]


def test_index_folder_is_kept_out_of_git(indexed_workspace):
    assert (indexed_workspace / ".cf" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert not list((indexed_workspace / ".cf").glob("index.db.tmp-*"))     # 途中のファイルを残さない


def test_index_records_stats_and_timings(searcher):
    st = searcher.status()
    assert st["stats"]["files"] == 13 and st["stats"]["chunks"] > 13
    assert {s["id"] for s in st["sources"]} == {"engine", "app", "docs"}
    assert set(st["stats"]["by_language"]) == {"cpp", "csharp", "markdown", "text"}
    assert st["timings"]["total_seconds"] >= 0 and st["index_bytes"] > 0
    assert st["embedding"]["model"] == "hash-256-test-only" and st["modes"] == ["hybrid", "keyword", "semantic"]
    assert "passages:" in status_agent(st)


def test_keyword_matches_identifier_parts(searcher):
    """識別子を分けた語でも当たる: 'job steal' で JobScheduler::Steal。"""
    resp = searcher.search("job scheduler steal", mode="keyword", top_k=3)
    top = resp.passages[0]
    assert (top["source_id"], top["path"]) == ("engine", "Core/JobScheduler.cpp")
    assert top["start_line"] <= 26 <= top["end_line"]
    assert top["scope"] == "kes::core::JobScheduler" and "Steal" in top["signature"]
    assert resp.mode == "keyword" and resp.timings["search_ms"] >= 0


def test_keyword_japanese_document(searcher):
    resp = searcher.search("書き込みの途中で電源が切れても失わない", mode="keyword", top_k=3)
    assert resp.passages[0]["path"] == "save-data.md"
    assert resp.passages[0]["start_line"] <= 20 <= resp.passages[0]["end_line"]


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
def test_every_mode_returns_ranked_passages(searcher, mode):
    resp = searcher.search("CRC-32 checksum of the payload", mode=mode, top_k=5)
    assert resp.mode == mode and 1 <= len(resp.passages) <= 5
    assert [p["rank"] for p in resp.passages] == list(range(1, len(resp.passages) + 1))
    for p in resp.passages:
        assert {"source_id", "path", "start_line", "end_line", "score", "text", "scope", "signature", "kind"} <= set(p)
        assert p["text"] and "file_id" not in p
    scores = [p["score"] for p in resp.passages]
    assert scores == sorted(scores, reverse=True)
    assert any(p["path"] in ("Save/SaveGameService.cs", "Core/Hash.cpp") for p in resp.passages[:3])


def test_source_filter(searcher):
    resp = searcher.search("CRC-32 checksum", mode="hybrid", top_k=8, source_ids=["engine"])
    assert resp.passages and {p["source_id"] for p in resp.passages} == {"engine"}
    with pytest.raises(ValueError):
        searcher.search("x", source_ids=["nope"])


def test_rrf_and_weights():
    assert [cid for cid, _ in rrf([[1, 2, 3], [3, 1, 4]], 60)] == [1, 3, 2, 4]
    # keyword の重みを下げると、semantic(2 つ目の並び)の順が勝つ
    assert [cid for cid, _ in rrf([[1, 2, 3], [3, 1, 4]], 60, [0.1, 1.0])][0] == 3


def test_hybrid_downweights_keyword_when_the_query_words_are_not_in_the_index(searcher):
    en = searcher.search("buffer pool acquire", mode="hybrid")
    ja = searcher.search("対戦相手を自動で探す仕組み", mode="hybrid")
    assert en.timings["keyword_weight"] == 1.0
    assert ja.timings["keyword_weight"] < 0.6


def test_keyword_only_index_falls_back(tmp_path):
    cfg = load_config(write_config(tmp_path))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    s = Searcher.from_config(cfg)
    assert s.status()["modes"] == ["keyword"] and s.status()["embedding"] is None
    resp = s.search("buffer pool", mode="hybrid")
    assert resp.mode == "keyword" and resp.requested_mode == "hybrid"
    assert resp.passages and "Fell back to keyword" in resp.notes[0]


def test_missing_model_builds_keyword_only_and_search_still_works(tmp_path, monkeypatch):
    """モデルが手元に無い環境: 索引は keyword だけで作られ、検索も動く。"""
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    cfg = load_config(write_config(tmp_path))
    logs: list[str] = []
    result = build_index(cfg, progress=False, log=logs.append)
    assert result["embedding"] is None and any("keyword" in line for line in logs)
    resp = Searcher.from_config(cfg).search("JobScheduler Steal")
    assert resp.mode == "keyword" and resp.passages[0]["path"] == "Core/JobScheduler.cpp"


def test_model_mismatch_falls_back(indexed_workspace, monkeypatch, tmp_path):
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    s = Searcher.from_config(load_config(indexed_workspace / ".cf" / "config.toml"))
    resp = s.search("buffer pool", mode="semantic")
    assert resp.mode == "keyword" and "not available" in resp.notes[0]


def test_no_index_raises(tmp_path):
    cfg = load_config(write_config(tmp_path))
    with pytest.raises(IndexError_):
        Searcher.from_config(cfg).search("x")


def test_stale_flag_after_edit(tmp_path, corpus_copy):
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    target = corpus_copy / "engine" / "Core" / "JobScheduler.cpp"
    target.write_text(target.read_text(encoding="utf-8") + "\n// edited after indexing\n", encoding="utf-8")
    os.utime(target, (time.time() + 5, time.time() + 5))
    resp = Searcher.from_config(cfg).search("JobScheduler Steal", mode="keyword", top_k=3)
    assert resp.passages[0].get("stale") is True
    assert "[changed since index]" in to_agent(resp)
    assert not any(p.get("stale") for p in resp.passages if p["path"] != "Core/JobScheduler.cpp")


def test_status_names_the_files_that_fell_back_to_lines(tmp_path, corpus_copy, searcher):
    """マクロで文法が崩れたファイルは、status に名前が出る(伏せる名前を探す手がかり)。"""
    entries = "\n".join(f"    (Entry{i}, {i}, \"entry number {i} of the table\")," for i in range(40))
    (corpus_copy / "engine" / "Core" / "Table.cpp").write_text(
        "namespace kes {\nint Before() { return 1; }\n\n"
        f"KES_TABLE_WITH_NAMES(Kind, unsigned,\n{entries}\n    (Count, 40))\n\n"
        "int After(int value)\n{\n    return value + 1;\n}\n}\n", encoding="utf-8")
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    st = Searcher.from_config(cfg).status()
    assert [(f["source_id"], f["path"]) for f in st["fallback_files"]] == [("engine", "Core/Table.cpp")]
    assert st["fallback_files"][0]["fallback_lines"] >= 40
    assert "engine:Core/Table.cpp" in status_human(st) and "attribute_macros" in status_human(st)
    assert searcher.status()["fallback_files"] == []          # 元のコーパスには、行で分けたファイルが無い
    assert "attribute_macros" not in status_human(searcher.status())


def test_reranker_hook(indexed_workspace):
    cfg = load_config(indexed_workspace / ".cf" / "config.toml")
    cfg.search.reranker = "reranker_for_test:reverse"
    resp = Searcher.from_config(cfg).search("buffer pool acquire", mode="keyword", top_k=3)
    plain = Searcher.from_config(load_config(indexed_workspace / ".cf" / "config.toml")).search(
        "buffer pool acquire", mode="keyword", top_k=30)
    assert len(resp.passages) == 3
    # 後段の reranker が並べ直した順(ここでは逆順)が、そのまま結果になる
    assert locs(resp)[0] == locs(plain)[min(len(plain.passages), cfg.search.rerank_candidates) - 1]


def test_agent_output_fits_the_budget(searcher):
    resp = searcher.search("buffer", mode="keyword", top_k=20)
    out = to_agent(resp, max_bytes=3000)
    assert len(out.encode("utf-8")) <= 3000
    assert "[1] " in out and "More matches (text not shown):" in out
    assert f"[{len(resp.passages)}] " in out            # 本文を省いた分も、場所は全部出す
    big = to_agent(resp, max_bytes=10 ** 9)
    assert len(big.encode("utf-8")) <= HARD_MAX_BYTES
    assert "Cite evidence as source:path:start-end" in out


def test_agent_output_names_location_scope_and_signature(searcher):
    out = to_agent(searcher.search("job scheduler steal", mode="keyword", top_k=1))
    assert "[1] engine:Core/JobScheduler.cpp:" in out and "scope: kes::core::JobScheduler" in out
    assert "bool JobScheduler::Steal(uint32_t thiefIndex, Job& outJob)" in out


def test_human_output(searcher):
    out = to_human(searcher.search("セーブデータの検査用の値", mode="keyword", top_k=2))
    assert "検索: セーブデータの検査用の値" in out and "save-data.md" in out


def test_embedding_text_adds_scope_and_drops_license_header():
    text = ("/*\n * Licensed under the MIT license. See LICENSE at the root.\n * SPDX-License-Identifier: MIT\n */\n"
            "#include <a.h>\nint f();\n")
    assert strip_license_header(text) == "#include <a.h>\nint f();\n"
    assert strip_license_header("// Explains the pool\nint f();\n").startswith("// Explains")
    out = embedding_text("Render/Pool.cpp", "kes::render::Pool", "void Pool::Trim()", "{\n  trim();\n}", 40)
    assert out.splitlines()[0] == "Render/Pool.cpp | kes::render::Pool | void Pool::Trim()"
