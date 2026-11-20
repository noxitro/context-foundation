"""索引と検索: keyword・semantic・hybrid、source の絞り込み、モデルが無いときの切り替え、出力の上限。

埋め込みは、テスト用の HashEmbedder(語のハッシュ。意味は見ない)なので、ここで確かめるのは
仕組みが通しで動くことと、keyword の一致のしかた。意味検索の質は、モデルの実物で別に確かめる。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing

import pytest

from context_foundation.config import ConfigError, load_config
from context_foundation.embed import HashEmbedder
from context_foundation.indexer import build_index, embedding_text, strip_license_header
from context_foundation.render import (FOOTER, FOOTER_NO_RELATED, HARD_MAX_BYTES, status_agent, status_human, to_agent,
                                      to_human, to_json)
from context_foundation.search import RELATED_SQL, SearchResponse, Searcher, freshness, rrf
from context_foundation.store import IndexError_
from context_foundation.textproc import MAX_QUERY_TOKENS, query_tokens, query_tokens_cut

from conftest import CORPUS, FIXTURES, write_config


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


def test_top_passage_lists_other_names_in_its_file(indexed_workspace):
    """[search] related(既定 8): 上位の passage に、同じファイルのほかの関数の名前を行の順に添える。上位に出ている
    passage の名前は除く。0 なら添えない(出力は 0.4.7 と同じ)。"""
    cfg_path = indexed_workspace / ".cf" / "config.toml"
    resp = Searcher.from_config(load_config(cfg_path)).search("job scheduler steal", mode="keyword", top_k=1)
    top = resp.passages[0]
    assert top["path"] == "Core/JobScheduler.cpp" and "Steal" in top["signature"]
    related = top["related"]
    assert "Steal" not in " ".join(related) and any("WorkerLoop" in s for s in related)
    assert any("Submit" in s for s in related)
    assert [i for i, s in enumerate(related) if "Submit" in s] < [i for i, s in enumerate(related) if "WorkerLoop" in s]
    assert "same file also has: " in to_agent(resp)

    cfg = load_config(cfg_path)
    cfg.search.related = 0
    resp = Searcher.from_config(cfg).search("job scheduler steal", mode="keyword", top_k=1)
    assert "related" not in resp.passages[0] and "same file also has: " not in to_agent(resp)


def test_footer_names_the_related_list_only_when_a_passage_has_one(indexed_workspace):
    """一覧が無い出力で、無い「same file also has」を探させない。一覧があるときの footer は FOOTER のまま。"""
    cfg_path = indexed_workspace / ".cf" / "config.toml"
    out = to_agent(Searcher.from_config(load_config(cfg_path)).search("job scheduler steal", mode="keyword", top_k=1))
    assert "same file also has: " in out and out.endswith("\n" + FOOTER + "\n")
    cfg = load_config(cfg_path)
    cfg.search.related = 0
    out = to_agent(Searcher.from_config(cfg).search("job scheduler steal", mode="keyword", top_k=1))
    assert "same file also has" not in out and out.endswith("\n" + FOOTER_NO_RELATED + "\n")


def test_names_beyond_the_limit_are_a_count_not_a_name(indexed_workspace):
    """limit を超えた分は related_more(数)。JSON の related は名前だけで、人と agent の出力にだけ「+N more」を出す。"""
    cfg_path = indexed_workspace / ".cf" / "config.toml"
    full = Searcher.from_config(load_config(cfg_path)).search("job scheduler steal", mode="keyword", top_k=1)
    names = full.passages[0]["related"]
    assert len(names) >= 2 and "related_more" not in full.passages[0]
    cfg = load_config(cfg_path)
    cfg.search.related = 1
    resp = Searcher.from_config(cfg).search("job scheduler steal", mode="keyword", top_k=1)
    top = resp.passages[0]
    assert top["related"] == names[:1] and top["related_more"] == len(names) - 1
    assert all("more" not in n for n in json.loads(to_json(resp))["passages"][0]["related"])
    assert f"same file also has: {names[0]}, +{len(names) - 1} more" in to_agent(resp)


def test_related_names_are_read_by_id_range_and_match_a_full_scan(indexed_workspace):
    """同じファイルの passage は、id の範囲(主キー)で読む(file_id には索引が無く、全部を読んでしまうため)。
    範囲で読んだものは、file_id で全部を探したものと同じ。"""
    db = sqlite3.connect(indexed_workspace / ".cf" / "index.db")
    try:
        plan = " ".join(str(r[-1]) for r in db.execute("EXPLAIN QUERY PLAN " + RELATED_SQL, (1, 2, 1)))
        assert "INTEGER PRIMARY KEY" in plan and "SCAN chunks" not in plan, plan
        for cid, fid, n in db.execute("SELECT c.id, c.file_id, f.chunks FROM chunks c JOIN files f ON f.id = c.file_id"):
            by_range = db.execute(RELATED_SQL, (cid - (n - 1), cid + (n - 1), fid)).fetchall()
            by_file = db.execute("SELECT kind, symbols FROM chunks WHERE file_id = ? ORDER BY start_line",
                                 (fid,)).fetchall()
            assert by_range == by_file and len(by_file) == n
    finally:
        db.close()


def test_negative_related_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="related"):
        load_config(write_config(tmp_path, extra="[search]\nrelated = -1\n"))


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
    from reranker_for_test import reverse
    cfg = load_config(indexed_workspace / ".cf" / "config.toml")
    resp = Searcher(cfg.index_path, cfg.search, cfg.embedding, reranker=reverse, rerank_candidates=30).search(
        "buffer pool acquire", mode="keyword", top_k=3)
    plain = Searcher.from_config(cfg).search("buffer pool acquire", mode="keyword", top_k=30)
    assert len(resp.passages) == 3
    # 後段の reranker が並べ直した順(ここでは逆順)が、そのまま結果になる
    assert locs(resp)[0] == locs(plain)[min(len(plain.passages), 30) - 1]


@pytest.mark.parametrize("key", ["reranker = 'this:s'", "rerank_candidates = 10"])
def test_workspace_config_cannot_name_a_reranker(tmp_path, key):
    """作業フォルダの設定からは、コードを読み込ませない(0.4.0 で項目ごと外した)。読み込む前に断る。"""
    import sys
    sys.modules.pop("this", None)
    with pytest.raises(ConfigError, match="0.4.0"):
        load_config(write_config(tmp_path, extra=f"[search]\n{key}\n"))
    assert "this" not in sys.modules


@pytest.mark.parametrize("value", ["../../victim.txt", "../index.db", "sub/../../x.db", "ABSOLUTE", ""])
def test_index_path_outside_cf_is_rejected(tmp_path, value):
    """[index] path は .cf の中だけ(作業フォルダの外のファイルを、索引で上書きさせない)。"""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    if value == "ABSOLUTE":
        value = victim.as_posix()
    ws = tmp_path / "a" / "ws"
    with pytest.raises(ConfigError, match=r"\[index\] path"):
        load_config(write_config(ws, extra=f"[index]\npath = '{value}'\n"))
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_index_path_inside_cf_still_works(tmp_path):
    cfg = load_config(write_config(tmp_path, extra="[index]\npath = 'sub/other.db'\n"))
    assert cfg.index_path == tmp_path / ".cf" / "sub" / "other.db"


def test_index_path_through_a_link_out_of_cf_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    cf = tmp_path / "ws" / ".cf"
    cf.mkdir(parents=True)
    try:
        (cf / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("この環境ではシンボリックリンクを作れない")
    with pytest.raises(ConfigError, match="外"):
        load_config(write_config(tmp_path / "ws", extra="[index]\npath = 'link/index.db'\n"))


@pytest.mark.parametrize("name", ["ws%20x", "ws%41b", "作業 #1?"])
def test_index_and_search_under_percent_escaped_folder(tmp_path, name):
    """フォルダ名の %20 などを、SQLite の URI で空白などと読まない。"""
    if "?" in name and os.name == "nt":
        name = name.replace("?", "")                   # Windows のファイル名には ? を使えない
    ws = tmp_path / name
    cfg = load_config(write_config(ws))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    resp = Searcher.from_config(cfg).search("JobScheduler Steal", mode="keyword", top_k=1)
    assert resp.passages[0]["path"] == "Core/JobScheduler.cpp"


def _relative_workspace(ws, corpus_copy):
    """ルートを作業フォルダからの相対で書いた作業フォルダ(ctxf init が書く形)。"""
    shutil.move(str(corpus_copy), str(ws / "corpus"))
    cf = ws / ".cf"
    cf.mkdir(parents=True)
    (cf / "config.toml").write_text("".join(
        f"[[source]]\nid = '{sid}'\nroot = 'corpus/{sid}'\n\n" for sid in ("engine", "app", "docs")),
        encoding="utf-8")
    cfg = load_config(cf / "config.toml")
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    return cfg


def _touch_edit(path):
    path.write_text(path.read_text(encoding="utf-8") + "\n// edited after indexing\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))


@pytest.mark.parametrize("how", ["rename", "copy"])
def test_stale_mark_follows_current_config_after_move(tmp_path, corpus_copy, how):
    """作業フォルダの名前を変えた・写した後も、今の作業フォルダのファイルで、変わったかを見る。"""
    ws = tmp_path / "wsA"
    _relative_workspace(ws, corpus_copy)
    moved = tmp_path / "wsB"
    if how == "rename":
        ws.rename(moved)
    else:
        shutil.copytree(ws, moved)
    _touch_edit(moved / "corpus" / "engine" / "Core" / "JobScheduler.cpp")
    resp = Searcher.from_config(load_config(moved / ".cf" / "config.toml")).search(
        "JobScheduler Steal", mode="keyword", top_k=3)
    assert resp.passages[0]["path"] == "Core/JobScheduler.cpp" and resp.passages[0].get("stale") is True
    assert not resp.notes


def test_missing_root_adds_note(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    cfg = _relative_workspace(ws, corpus_copy)
    (ws / "corpus" / "engine").rename(ws / "corpus" / "engine-gone")
    resp = Searcher.from_config(cfg).search("JobScheduler Steal", mode="keyword", top_k=3)
    assert resp.passages[0]["source_id"] == "engine"
    assert any("cannot check whether files changed" in n and "engine" in n for n in resp.notes)
    assert "cannot check whether files changed" in to_agent(resp)


def test_very_long_query_is_capped(searcher):
    """語が多すぎる質問でも、SQLite の変数の上限に当たらない(重なりを除いた先頭の語だけを使う)。"""
    query = " ".join(f"word{i}" for i in range(40_000))
    assert len(query_tokens(query)) == MAX_QUERY_TOKENS
    t = time.perf_counter()
    resp = searcher.search(query, mode="hybrid", top_k=3)
    assert resp.mode == "hybrid" and time.perf_counter() - t < 30
    assert query_tokens("job scheduler steal") == ["job", "scheduler", "steal"]


def test_long_query_note_starts_after_exactly_the_cap(searcher):
    """ちょうど上限(256 語)の質問は切らず(note なし)、257 語で初めて切る。"""
    import itertools
    import string
    words = ["zx" + a + b + "k" for a, b in itertools.product(string.ascii_lowercase, repeat=2)][:MAX_QUERY_TOKENS + 1]
    assert query_tokens(" ".join(words)) == words[:MAX_QUERY_TOKENS]
    at_cap = " ".join(words[:MAX_QUERY_TOKENS])
    assert query_tokens_cut(at_cap) == (words[:MAX_QUERY_TOKENS], False)
    assert query_tokens_cut(" ".join(words)) == (words[:MAX_QUERY_TOKENS], True)
    assert not any(n.startswith("long query") for n in searcher.search(at_cap, mode="keyword").notes)
    assert any(n.startswith("long query") for n in searcher.search(" ".join(words), mode="keyword").notes)


class _RebuildingEmbedder(HashEmbedder):
    """質問を埋め込んでいる間に、索引の作り直しが終わる(MCP サーバーが開いたままの間に `ctxf index` した)。"""

    def __init__(self, on_query):
        super().__init__()
        self.on_query = on_query

    def embed_query(self, text):
        if self.on_query:
            hook, self.on_query = self.on_query, None
            hook()
        return super().embed_query(text)


def _strip_build_id(index_path):
    """0.3 で作った索引の形にする(build_id と query_prefix が無い)。"""
    with closing(sqlite3.connect(str(index_path))) as db:
        db.execute("DELETE FROM meta WHERE key = 'build_id'")
        emb = json.loads(db.execute("SELECT value FROM meta WHERE key = 'embedding'").fetchone()[0])
        emb.pop("query_prefix", None)
        db.execute("UPDATE meta SET value = ? WHERE key = 'embedding'", (json.dumps(emb),))
        db.commit()


@pytest.mark.parametrize("old_index", [False, True])
@pytest.mark.parametrize("sources", [None, ["docs"]])
def test_rebuild_during_query_embedding(tmp_path, corpus_copy, old_index, sources):
    """作り直しで passage の id が振り直されても、前の索引のベクトルの id で新しい索引を引かない。"""
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, progress=False, log=lambda s: None)
    if old_index:
        _strip_build_id(cfg.index_path)

    def rebuild():
        # 名前の順で先頭に来るファイルを足す(後ろの passage の id が、すべてずれる)
        (corpus_copy / "engine" / "AAA_First.cpp").write_text(
            "".join(f"int first_function_{i}(int x)\n{{\n    return x * {i};\n}}\n\n" for i in range(30)),
            encoding="utf-8")
        build_index(cfg, progress=False, log=lambda s: None)
        if old_index:
            _strip_build_id(cfg.index_path)

    query = "save data checksum written before power loss"
    s = Searcher.from_config(cfg, embedder=_RebuildingEmbedder(None))
    s.search(query, mode="semantic", top_k=1, source_ids=sources)            # ベクトルを読み込んでおく
    s._embedder.on_query = rebuild
    resp = s.search(query, mode="semantic", top_k=5, source_ids=sources)
    ref = Searcher.from_config(cfg).search(query, mode="semantic", top_k=5, source_ids=sources)
    assert locs(resp) == locs(ref) and resp.mode == "semantic"


class _OtherSettingsEmbedder(HashEmbedder):
    def __init__(self, **extra):
        super().__init__()
        self.extra = extra

    def info(self):
        return dict(super().info(), **self.extra)


@pytest.mark.parametrize("extra", [{"pooling": "cls"}, {"max_tokens": 128}, {"onnx_file": "onnx/other.onnx"}])
def test_changed_embedding_settings_without_rebuild_fall_back(indexed_workspace, extra):
    """埋め込みの作り方(pooling・max_tokens・onnx_file など)が索引と違えば、keyword にして作り直しを勧める。"""
    cfg = load_config(indexed_workspace / ".cf" / "config.toml")
    s = Searcher.from_config(cfg, embedder=_OtherSettingsEmbedder(**extra))
    resp = s.search("buffer pool", mode="hybrid")
    assert resp.mode == "keyword" and resp.passages
    assert any(next(iter(extra)) in n and "rebuild the index" in n for n in resp.notes)
    assert s.warm_up() is False


def test_changed_query_prefix_without_rebuild_falls_back(indexed_workspace):
    from types import SimpleNamespace
    cfg = load_config(indexed_workspace / ".cf" / "config.toml")
    emb = HashEmbedder()
    emb.spec = SimpleNamespace(query_prefix="query: ", passage_prefix="")
    resp = Searcher.from_config(cfg, embedder=emb).search("buffer pool", mode="semantic")
    assert resp.mode == "keyword" and any("query_prefix" in n for n in resp.notes)


def test_index_from_0_3_without_new_meta_still_searches(tmp_path):
    """0.3 で作った索引(build_id・query_prefix が無い)も、作り直さずに使える。"""
    cfg = load_config(write_config(tmp_path))
    build_index(cfg, progress=False, log=lambda s: None)
    _strip_build_id(cfg.index_path)
    s = Searcher.from_config(cfg)
    assert s.warm_up() is True
    resp = s.search("job scheduler steal", mode="hybrid", top_k=3)
    assert resp.mode == "hybrid" and not resp.notes and resp.passages


def test_index_schema_and_query_cap_stay_as_in_0_3_1():
    """索引の形式の版と、keyword の検索に使う語の数の上限は 0.3.1 と同じ(上げると 0.3.1 の索引が作り直しになる・
    長い質問の順位が変わる)。定数どうしで比べず、数で書く。"""
    from context_foundation import INDEX_SCHEMA_VERSION
    assert INDEX_SCHEMA_VERSION == 1
    assert MAX_QUERY_TOKENS == 256
    assert len(query_tokens(" ".join(f"word{i}" for i in range(300)))) == 256


def test_default_output_limit_and_top_k_stay_as_in_0_3_1(tmp_path):
    """CLI と MCP が使う、設定の既定の出力の上限と件数(測った構成の出力を決める値)。数で書く。"""
    cfg = load_config(write_config(tmp_path))
    assert cfg.search.max_output_bytes == 12_000
    assert cfg.search.top_k == 8


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


# --- エージェント向けの出力: 変わった・消えたファイル(CRIT-3)、予算(MCP-6・MCP-7)、ルート(MCP-3)

def _resp(passages, **kw):
    return SearchResponse(query="q", mode="hybrid", requested_mode="hybrid", passages=passages,
                          index={"created_at": "2026-01-01T00:00:00+0900", "chunks": 99}, **kw)


def _p(rank, text, path="Core/A.cpp", sig="", **extra):
    return {"source_id": "engine", "path": path, "start_line": 1, "end_line": text.count("\n") + 1,
            "kind": "function", "scope": "", "signature": sig, "text": text, "rank": rank, **extra}


def location_of(p):
    return f"{p['path']}:{p['start_line']}-{p['end_line']}"


def _shown_and_dropped(out: str) -> tuple[list[int], int]:
    """出力に出た順位(出た順)と、`(+N more matches, not shown)` の N。"""
    import re
    shown = [int(m) for m in re.findall(r"^\[(\d+)\] ", out, re.M)]
    m = re.search(r"^\(\+(\d+) more matches, not shown\)$", out, re.M)
    return shown, int(m.group(1)) if m else 0


def test_deleted_file_is_marked_apart_from_changed_and_its_text_is_not_shown(tmp_path, corpus_copy):
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    _touch_edit(corpus_copy / "engine" / "Core" / "JobScheduler.cpp")
    (corpus_copy / "engine" / "Render" / "BufferPool.cpp").unlink()
    resp = Searcher.from_config(cfg).search("JobScheduler Steal BufferPool Acquire", mode="keyword", top_k=8)
    by_path = {p["path"]: p for p in resp.passages}
    assert by_path["Core/JobScheduler.cpp"].get("stale") and not by_path["Core/JobScheduler.cpp"].get("missing")
    assert by_path["Render/BufferPool.cpp"].get("missing") and by_path["Render/BufferPool.cpp"].get("stale")
    out = to_agent(resp)
    gone = by_path["Render/BufferPool.cpp"]
    # 消えたファイルは、場所だけの行に印つきで出し、本文は出さない
    assert f"[{gone['rank']}] engine:{location_of(gone)}  ({gone['kind']})  [file no longer exists]" in out
    deleted = [p for p in resp.passages if p.get("missing")]
    listed = out.split("More matches (text not shown):\n")[1]
    for p in deleted:
        line = next(x for x in out.splitlines() if x.startswith(f"[{p['rank']}] "))
        assert line in listed.splitlines() and "[file no longer exists]" in line     # 本文つきの block の側に無い
        assert not out.split(line + "\n", 1)[1].startswith("```")
    assert "Prefer a retired buffer over a fresh allocation" not in out           # BufferPool.cpp の本文にだけある行
    assert out.count("```") == 2 * sum(1 for p in resp.passages if not p.get("missing"))
    # 人が読む形は印を付けて索引のときの本文を出し、JSON は本文に missing: true を付ける(文書に書いた 3 つの形)
    human = to_human(resp)
    assert "※索引の後に消えたファイル" in human and "Prefer a retired buffer over a fresh allocation" in human
    as_json = [p for p in resp.to_dict()["passages"] if p["path"] == "Render/BufferPool.cpp"]
    assert as_json and all(p["missing"] is True and p["stale"] is True and p["text"] for p in as_json)
    n = sum(1 for p in resp.passages if p.get("stale"))
    assert f"stale: {n} of {len(resp.passages)} passages are from files changed or deleted after the index" in out
    assert "check the current lines before citing them" in out and "do not cite them" in out
    # 予算に収まらず場所だけになった passage にも、印が付く
    small = to_agent(resp, max_bytes=2500)
    listed = small.split("More matches (text not shown):\n")[1].split("\n\n")[0].splitlines()
    assert any("  (function)  [changed since index]" in line for line in listed)
    assert len(small.encode("utf-8")) <= 2500
    assert "※索引の後に消えたファイル" in to_human(resp)


def test_output_without_stale_passages_has_no_stale_lines(searcher):
    out = to_agent(searcher.search("job scheduler steal", mode="keyword", top_k=3))
    assert "stale:" not in out and "[changed since index]" not in out and "do not cite them" not in out
    # 上位 3 件で JobScheduler.cpp の名前は出尽くすので、「same file also has」の一覧は無い
    assert "same file also has" not in out and out.endswith("\n" + FOOTER_NO_RELATED + "\n")


def test_oversized_passage_does_not_hide_later_passages():
    """改行の無い長い段落(14 KB)が 1 位でも、後ろの passage の本文は予算の中で出る。"""
    big = _p(1, "## 概要\n" + "描画パイプラインのコマンドバッファを再利用する。" * 600 + "\n## 補足", path="docs/design.md")
    small = [_p(i, f"void Small{i}()\n{{\n    return;\n}}", path=f"Core/S{i}.cpp") for i in range(2, 9)]
    out = to_agent(_resp([big] + small))
    assert len(out.encode("utf-8")) <= 12_000
    assert all(f"void Small{i}()" in out for i in range(2, 9))
    assert "[1] engine:docs/design.md:" in out and "## 概要" in out


def test_single_line_longer_than_the_room_is_cut_at_a_character_boundary():
    first = _p(1, "x" * 200)
    long_one = _p(2, "描画" * 4000 + "\n次の行")       # 1 行が 24 KB
    out = to_agent(_resp([first, long_one]), max_bytes=3000)
    assert len(out.encode("utf-8")) <= 3000
    assert "(line truncated, +1 more lines, not shown)" in out and "描画描画" in out
    assert "�" not in out                        # 文字の途中で切っていない(置き換えの文字が無い)
    cut = out.split("[2] ")[1].split("```\n")[1].split("\n")[0]
    assert cut and cut == ("描画" * 4000)[:len(cut)]   # 切った行は、元の行の先頭そのもの


@pytest.mark.parametrize("max_bytes", [12_000, 18_000])
@pytest.mark.parametrize("path, sig", [
    ("/".join(["very_long_directory_name_for_engine_modules"] * 6) + "/File.cpp", "void Long::Function(int)" * 4),
    ("/".join(["描画パイプラインの設計と実装のメモ"] * 4) + "/設計.md", "描画パイプラインのコマンドバッファの見出し" * 3)])
def test_agent_output_fits_the_budget_with_long_paths(max_bytes, path, sig):
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    resp = _resp([_p(i, text, path=path, sig=sig) for i in range(1, 21)])
    out = to_agent(resp, max_bytes=max_bytes)
    assert len(out.encode("utf-8")) <= max_bytes
    assert "[1] " in out and "More matches (text not shown):" in out
    # 場所は全部出すか、出せなかった件数を出す
    shown, dropped = _shown_and_dropped(out)
    assert shown == list(range(1, len(shown) + 1)) and len(shown) + dropped == 20


def _near_budget_response():
    """本文が上限近くまで入り、場所だけの行が 150 バイトより短い 8 件(0.3.1 の見積もりで収まっていた形)。"""
    return _resp([_p(i, "\n".join(f"    value_{i}_{n} = compute_{i}({n}, buffer_pool_slot_{n});" for n in range(38)),
                     path=f"Render/Pass{i}.cpp", sig=f"void Pass{i}::Execute(Context& ctx)") for i in range(1, 9)])


def test_output_that_fit_in_0_3_1_is_the_031_output():
    """0.3.1 で上限に収まっていた出力は 1 バイトも変えない(正解は 0.3.1 の to_agent で作ったもの)。1 回目の
    並べ方(場所だけの行を 1 件 150 バイトとみる見積もり)を変えると、ここで落ちる。"""
    out = to_agent(_near_budget_response())
    golden = FIXTURES / "golden" / "agent_near_budget.txt"
    if os.environ.get("CF_UPDATE_GOLDEN") == "1":       # 文言を意図して変えたとき(test_mcp_cli の assert_golden と同じ)
        golden.write_bytes(out.encode("utf-8"))
    assert out.encode("utf-8") == golden.read_bytes()
    # 0.4.9 で footer を長くした分(関連する処理を見てから答える文)、本文の見える行が 2 行減った
    assert "(+23 more lines, not shown)" in out and "More matches (text not shown):" in out


@pytest.mark.parametrize("over", [1, 2, 7, 40])
def test_output_just_over_the_budget_is_cut_to_fit(over):
    """全部を本文で出すと上限をわずかに超える 8 件: 最後の passage を行で切って、上限に収める。"""
    body = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(27))
    passages = [_p(i, body, path=f"Core/P{i}.cpp") for i in range(1, 9)]
    pad = 12_000 + over - len(to_agent(_resp(passages), max_bytes=HARD_MAX_BYTES).encode("utf-8"))
    assert pad > 1
    passages[-1] = _p(8, body + "\n" + "x" * (pad - 1), path="Core/P8.cpp")
    assert len(to_agent(_resp(passages), max_bytes=HARD_MAX_BYTES).encode("utf-8")) == 12_000 + over
    out = to_agent(_resp(passages))
    assert len(out.encode("utf-8")) <= 12_000
    assert _shown_and_dropped(out) == (list(range(1, 9)), 0)


@pytest.mark.parametrize("max_bytes", [12_000, 18_000])
def test_deleted_file_ranked_first_keeps_its_line_within_the_budget(max_bytes):
    """1 位が消えたファイルで、残りの本文が予算を使い切る: 上限に収め、消えたファイルの場所の行も出す。"""
    gone = _p(1, "int Gone() { return 0; }", path="Render/BufferPool.cpp", sig="int Gone()", stale=True, missing=True)
    for lines in range(40, 141, 3):
        body = "\n".join(f"    step {n}: copy the staging buffer into the frame ring" for n in range(lines))
        out = to_agent(_resp([gone] + [_p(i, body, path=f"Render/Pass{i}.cpp") for i in range(2, 9)]),
                       max_bytes=max_bytes)
        assert len(out.encode("utf-8")) <= max_bytes, lines
        assert "\n[1] engine:Render/BufferPool.cpp:1-1  (function)  [file no longer exists]" in out, lines


@pytest.mark.parametrize("over", [1, 2, 7, 40])
def test_deleted_file_line_survives_an_output_just_over_the_budget(over):
    """1 位が消えたファイル(シグネチャなし)で、全部を出すと上限をわずかに超える: 最後の本文を切って収め、
    消えたファイルの場所の行は落とさない(ちょうど 1 バイト超えるときも)。"""
    gone = _p(1, "int Gone() { return 0; }", path="Render/BufferPool.cpp", stale=True, missing=True)
    body = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(27))
    passages = [gone] + [_p(i, body, path=f"Core/P{i}.cpp") for i in range(2, 9)]
    pad = 12_000 + over - len(to_agent(_resp(passages), max_bytes=HARD_MAX_BYTES).encode("utf-8"))
    assert pad > 1
    passages[-1] = _p(8, body + "\n" + "x" * (pad - 1), path="Core/P8.cpp")
    assert len(to_agent(_resp(passages), max_bytes=HARD_MAX_BYTES).encode("utf-8")) == 12_000 + over
    out = to_agent(_resp(passages))
    assert len(out.encode("utf-8")) <= 12_000
    assert "\n[1] engine:Render/BufferPool.cpp:1-1  (function)  [file no longer exists]" in out
    assert "more matches, not shown" not in out


def test_deleted_file_line_is_counted_when_no_location_line_fits():
    """場所だけの行も入りきらない小さい上限では、消えたファイルの行も、ほかの行と同じく後ろから件数にまとめる
    (上限は守り、頭の stale: の行は残す)。"""
    path = "/".join(["描画パイプラインの設計と実装のメモ"] * 4) + "/設計.md"
    passages = [_p(i, "本文の 1 行", path=path, sig="描画のコマンド" * 5) for i in range(1, 50)]
    passages.append(_p(50, "int Gone() { return 0; }", path=path, stale=True, missing=True))
    out = to_agent(_resp(passages), max_bytes=1000)
    assert len(out.encode("utf-8")) <= 1000
    assert "\nstale: 1 of 50 passages are from files changed or deleted" in out
    shown, dropped = _shown_and_dropped(out)
    assert dropped > 0 and 50 not in shown and len(shown) + dropped == 50
    assert "(function)  [file no longer exists]" not in out


def test_output_exceeds_the_budget_only_by_the_more_matches_lines():
    """上限を超えるのは、決まって出す部分に `More matches` の見出しと件数の行(合わせて 61 バイト)を足した大きさが
    上限を超えるときだけ。決まって出す部分が 991 バイト(上限 1,000 の内側)でも、出力は 1,052 バイトになる。"""
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    passages = [_p(i, text, path=f"Core/P{i}.cpp", stale=True) for i in range(1, 9)]
    for n in range(1, 1000):
        out = to_agent(_resp(passages, roots={"engine": "r" * n}), max_bytes=1000)
        if _shown_and_dropped(out) == ([], 8) and len(out.encode("utf-8")) == 991 + 61:
            break
    else:
        pytest.fail("no roots length gives a fixed part of 991 bytes")
    lines = out.split("\n")
    assert lines[0].startswith("context-foundation search: 8 passages")
    assert lines[1].startswith("stale: 8 of 8 passages")
    assert lines[2:6] == ["", "More matches (text not shown):", "(+8 more matches, not shown)", ""]
    assert lines[6].startswith("roots: engine = rrr") and lines[7].startswith("Cite evidence") and lines[8:] == [""]


def test_agent_output_properties_on_random_responses():
    """乱数で作った応答(種は固定。変わった・消えたファイル、ルート、行番号つき): 上限、コードの囲みの対、
    順位の順、出た件数と落とした件数の合計。"""
    import random
    rng = random.Random(20261117)
    words = ["buffer", "pool", "描画", "パイプライン", "job", "very_long_directory_name"]
    for _ in range(500):
        passages = []
        for rank in range(1, rng.randint(1, 20) + 1):
            if rng.random() < 0.1:              # 改行の無い長い段落
                text = rng.choice(words) * rng.randint(50, 400)
            else:
                text = "\n".join(" ".join(rng.choice(words) for _ in range(rng.randint(1, 10)))
                                 for _ in range(rng.randint(1, 40)))
            mark = rng.choice([{}] * 8 + [{"stale": True}, {"stale": True, "missing": True}])
            path = "/".join(rng.choice(words) for _ in range(rng.randint(1, 10))) + ".cpp"
            passages.append(_p(rank, text, path=path, sig=rng.choice(["", "void F()", "描画のコマンド" * 5]), **mark))
        roots = {"engine": "../" + "/".join(rng.choice(words) for _ in range(rng.randint(1, 6)))} \
            if rng.random() < 0.3 else {}
        max_bytes = rng.choice([2_000, 3_000, 6_000, 12_000, 18_000])
        out = to_agent(_resp(passages, roots=roots), max_bytes=max_bytes, line_numbers=rng.random() < 0.2)
        assert len(out.encode("utf-8")) <= max_bytes
        assert out.count("```") % 2 == 0
        shown, dropped = _shown_and_dropped(out)
        assert sorted(shown) == list(range(1, len(shown) + 1)) and len(shown) + dropped == len(passages)
        body, _, listed = out.partition("More matches (text not shown):")
        for part in (body, listed):
            ranks = _shown_and_dropped(part)[0]
            assert ranks == sorted(ranks)
        for p in passages:
            if p.get("missing") and p["rank"] in shown:
                assert f"[{p['rank']}] " not in body                      # 消えたファイルの本文は出さない


def test_second_layout_keeps_room_for_long_location_lines():
    """場所だけの行が長い(1 行 250 バイトほど)20 件: 2 回目の並べ直しで本文を減らし、場所は 1 件も落とさない
    (0.3 の見積もりの 150 バイトのままだと、場所を後ろから落とすことになる)。"""
    path = "/".join(["very_long_directory_name_for_engine_modules"] * 4) + "/File.cpp"
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    out = to_agent(_resp([_p(i, text, path=path, sig="void Long::Function(int)" * 4) for i in range(1, 21)]))
    assert len(out.encode("utf-8")) <= 12_000
    assert _shown_and_dropped(out) == (list(range(1, 21)), 0)
    assert all(f"[{i}] " in out.split("More matches (text not shown):")[1] for i in range(15, 21))


def test_last_resort_drops_locations_and_says_how_many():
    """日本語の長いパスとシグネチャの 20 件を上限 3,000 で: 後ろの場所の行を落とし、落とした件数を出す。"""
    path = "/".join(["描画パイプラインの設計と実装のメモ"] * 4) + "/設計.md"
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    out = to_agent(_resp([_p(i, text, path=path, sig="描画パイプラインのコマンドバッファの見出し" * 3)
                          for i in range(1, 21)]), max_bytes=3000)
    assert len(out.encode("utf-8")) <= 3000 and "more matches, not shown)" in out
    shown, dropped = _shown_and_dropped(out)
    assert dropped > 0 and shown == list(range(1, len(shown) + 1)) and len(shown) + dropped == 20


def test_long_roots_are_counted_in_the_budget():
    roots = {"engine": "../" + "r" * 300, "docs": "../" + "d" * 300}
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    out = to_agent(_resp([_p(i, text, path=f"Core/P{i}.cpp", sig=f"void P{i}()") for i in range(1, 21)], roots=roots),
                   max_bytes=3000)
    assert len(out.encode("utf-8")) <= 3000 and "roots: engine = ../" + "r" * 300 + ", docs = ../" in out
    shown, dropped = _shown_and_dropped(out)
    assert len(shown) + dropped == 20


def test_hard_limit_stays_below_the_size_copilot_moves_to_a_file():
    """上限の最大は 18,000 バイト(Copilot CLI は 20,480 バイトを超える道具の結果をファイルに退かせる)。"""
    assert HARD_MAX_BYTES == 18_000 and HARD_MAX_BYTES < 20_480
    text = "\n".join(f"    line {n} of the passage body that is long enough" for n in range(30))
    resp = _resp([_p(i, text, path=f"Core/P{i}.cpp") for i in range(1, 21)])     # 本文の合計は 30 KB ほど
    assert 15_000 < len(to_agent(resp, max_bytes=10 ** 9).encode("utf-8")) <= 18_000


@pytest.mark.parametrize("max_bytes", [1, 1000])
def test_smallest_budget_with_many_long_locations(max_bytes):
    """上限の最小(1,000 バイト。それより小さい値は 1,000 にする)で、場所だけの行も入りきらない 50 件。"""
    path = "/".join(["描画パイプラインの設計と実装のメモ"] * 4) + "/設計.md"
    resp = _resp([_p(i, "本文の 1 行", path=path, sig="描画のコマンド" * 5) for i in range(1, 51)])
    out = to_agent(resp, max_bytes=max_bytes)
    assert len(out.encode("utf-8")) <= 1000 and "more matches, not shown)" in out
    shown, dropped = _shown_and_dropped(out)
    assert len(shown) + dropped == 50


def test_roots_line_only_when_a_source_is_not_the_workspace(tmp_path, corpus_copy, searcher):
    # フィクスチャの作業フォルダ: ルートは作業フォルダの外
    resp = searcher.search("job scheduler steal", mode="keyword", top_k=2)
    roots_line = next(x for x in to_agent(resp).splitlines() if x.startswith("roots: "))
    # 作業フォルダの外のルートは、設定の絶対パスのまま出る(文書に書いた動き。モデルに送られる)
    assert f"engine = {CORPUS / 'engine'}" in roots_line and os.path.isabs(resp.roots["engine"])
    # ルートが作業フォルダそのもの(測った形)なら、roots の行は無い
    ws = tmp_path / "ws"
    shutil.copytree(corpus_copy / "engine", ws)
    (ws / ".cf").mkdir()
    (ws / ".cf" / "config.toml").write_text("[[source]]\nid = 'ws'\nroot = '.'\n", encoding="utf-8")
    cfg = load_config(ws / ".cf" / "config.toml")
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    resp = Searcher.from_config(cfg).search("job scheduler steal", mode="keyword", top_k=2)
    assert resp.roots == {} and "roots:" not in to_agent(resp)
    assert "ws = . (" in status_agent(Searcher.from_config(cfg).status())


def test_status_lists_source_roots_relative_to_the_workspace(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    cfg = _relative_workspace(ws, corpus_copy)
    st = Searcher.from_config(cfg).status()
    assert "sources: engine = corpus/engine (7 files" in status_agent(st)
    assert "Open source:path as <root>/<path>" in status_agent(st)
    resp = Searcher.from_config(cfg).search("job scheduler steal", mode="keyword", top_k=2)
    assert "roots: engine = corpus/engine" in to_agent(resp)
    # 作業フォルダを移した後は、今のルートを出し、索引を作ったときのルートも human に出す
    moved = tmp_path / "moved"
    ws.rename(moved)
    st = Searcher.from_config(load_config(moved / ".cf" / "config.toml")).status()
    assert st["sources"][0]["root"] == str(moved / "corpus" / "engine")
    assert "索引を作ったときは" in status_human(st)


def test_stale_check_reads_files_under_long_paths(tmp_path):
    ws = tmp_path / "w"
    deep = ws
    for i in range(6):
        deep = deep / (f"{i}_" + "d" * 48)
    # Windows では拡張の形を、確かめる対象の関数を使わずに書く(作れなければ skip ではなく失敗にする)
    target = "\\\\?\\" + os.path.abspath(deep / "Deep.cpp") if sys.platform == "win32" else str(deep / "Deep.cpp")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("int deep_function() { return 7; }\n")
    (ws / ".cf").mkdir()
    (ws / ".cf" / "config.toml").write_text("[[source]]\nid = 'w'\nroot = '.'\n", encoding="utf-8")
    cfg = load_config(ws / ".cf" / "config.toml")
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    resp = Searcher.from_config(cfg).search("deep_function", mode="keyword", top_k=1)
    assert resp.passages and not resp.passages[0].get("stale")


def test_status_check_counts_changed_added_and_deleted_files(tmp_path, corpus_copy):
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    assert freshness(cfg)["changed"] == freshness(cfg)["added"] == freshness(cfg)["deleted"] == 0
    _touch_edit(corpus_copy / "engine" / "Core" / "JobScheduler.cpp")
    (corpus_copy / "engine" / "Core" / "New.cpp").write_text("int added_after_index() { return 1; }\n",
                                                              encoding="utf-8")
    (corpus_copy / "docs" / "jobs.md").unlink()
    fr = freshness(cfg)
    assert (fr["changed"], fr["added"], fr["deleted"]) == (1, 1, 1)
    assert fr["examples"] == {"changed": ["engine:Core/JobScheduler.cpp"], "added": ["engine:Core/New.cpp"],
                              "deleted": ["docs:jobs.md"]}
    st = dict(Searcher.from_config(cfg).status(), freshness=fr)
    assert "since the index: 1 files changed, 1 added, 1 deleted (e.g. engine:Core/JobScheduler.cpp" in status_agent(st)
    assert "変わった 1、増えた 1、消えた 1" in status_human(st)
    assert "since the index" not in status_agent(Searcher.from_config(cfg).status())    # --check のときだけ
    # 今の版で作った索引なら、版の違いの注は出ない
    assert "was built by tool" not in status_agent(st) and "取り込む規則が版で" not in status_human(st)


def test_status_check_notes_an_index_built_by_another_version(tmp_path, corpus_copy):
    """前の版で作った索引(作り直さずに使える)では、増えた・消えたに取り込む規則の違いの分も入りうると添える。"""
    from context_foundation import __version__
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    with closing(sqlite3.connect(cfg.index_path)) as db, db:
        db.execute("UPDATE meta SET value = ? WHERE key = 'tool_version'", (json.dumps("0.3.1"),))
    unchanged = dict(Searcher.from_config(cfg).status(), freshness=freshness(cfg))
    assert "was built by tool" not in status_agent(unchanged)              # 増えた・消えたが 0 なら添えない
    assert "取り込む規則が版で" not in status_human(unchanged)
    (corpus_copy / "engine" / "Core" / "New.cpp").write_text("int added_after_index() { return 1; }\n",
                                                              encoding="utf-8")
    st = dict(Searcher.from_config(cfg).status(), freshness=freshness(cfg))
    line = status_agent(st).splitlines()[-1]
    assert line.startswith("since the index: 0 files changed, 1 added, 0 deleted")
    assert f"; the index was built by tool 0.3.1 (this is {__version__})" in line
    assert '"added" and "deleted" may include files that did not change' in line
    assert f"索引は道具の版 0.3.1 で作ったものです(今は {__version__})" in status_human(st)
    assert "since the index" not in status_agent(Searcher.from_config(cfg).status())    # --check のときだけ
    # 消えただけ(増えたは 0)でも添える
    (corpus_copy / "engine" / "Core" / "New.cpp").unlink()
    (corpus_copy / "docs" / "jobs.md").unlink()
    st = dict(Searcher.from_config(cfg).status(), freshness=freshness(cfg))
    line = status_agent(st).splitlines()[-1]
    assert line.startswith("since the index: 0 files changed, 0 added, 1 deleted")
    assert f"; the index was built by tool 0.3.1 (this is {__version__})" in line
    assert f"索引は道具の版 0.3.1 で作ったものです(今は {__version__})" in status_human(st)


def test_zero_hit_human_message_names_the_index_date(searcher):
    resp = searcher.search("zzzqqq nothingmatches", mode="keyword")
    assert not resp.passages
    assert f"索引({resp.index['created_at']})の後に増えたファイルは" in to_human(resp)
    assert f"index of {resp.index['created_at']}" in to_agent(resp)


@pytest.mark.parametrize("bad", ["//198.51.100.7/share/a.cpp", "../../outside.txt", "Core/../../../outside.txt",
                                 "ABSOLUTE"] + (["C:/outside.txt", "C:outside.txt"] if os.name == "nt" else []))
def test_stale_check_does_not_touch_paths_outside_the_root(tmp_path, corpus_copy, monkeypatch, bad):
    """索引の path が絶対パス・UNC・.. を含むとき(仕込まれた索引)、ファイルを見ずに stale にする。"""
    cfg = load_config(write_config(tmp_path, corpus_copy))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    if bad == "ABSOLUTE":
        bad = outside.as_posix()
    if os.name == "nt" and bad.startswith("//"):
        bad = bad.replace("/", "\\")
    with closing(sqlite3.connect(cfg.index_path)) as db, db:
        fid = db.execute("SELECT id FROM files WHERE path = 'Core/JobScheduler.cpp'").fetchone()[0]
        db.execute("UPDATE files SET path = ? WHERE id = ?", (bad, fid))
        db.execute("UPDATE chunks SET path = ? WHERE file_id = ?", (bad, fid))
    asked: list[str] = []
    real_stat = os.stat

    def recording_stat(path, *a, **kw):
        asked.append(os.fspath(path))
        if "198.51.100.7" in os.fspath(path):          # 見に行ってしまっても、ネットワークには出ない
            raise FileNotFoundError(path)
        return real_stat(path, *a, **kw)
    monkeypatch.setattr(os, "stat", recording_stat)
    resp = Searcher.from_config(cfg).search("JobScheduler Steal", mode="keyword", top_k=5)
    monkeypatch.undo()
    planted = [p for p in resp.passages if p["path"] == bad]
    assert planted and all(p.get("stale") and not p.get("missing") for p in planted)
    assert not any("198.51.100.7" in a or "outside" in a for a in asked), asked
    assert not any(p.get("stale") for p in resp.passages if p["path"] != bad)      # ふつうの path は今までどおり


def test_long_query_keeps_the_glossary_terms_and_says_it_was_cut(searcher, monkeypatch):
    """語の上限に当たっても、対応表で足した語(注に出す語)は keyword の検索に入る。切ったことは注で知らせる。"""
    import context_foundation.search as search_mod
    searched: list[list[str]] = []
    real = search_mod.fts_query

    def spy(tokens):
        searched.append(list(tokens))
        return real(tokens)
    monkeypatch.setattr(search_mod, "fts_query", spy)
    query = " ".join(f"zz{i}q" for i in range(200)) + " セーブデータ"
    resp = searcher.search(query, mode="keyword", top_k=3)
    assert resp.added_terms and searched
    used = searched[-1]
    assert len(used) <= MAX_QUERY_TOKENS
    for t in query_tokens(" ".join(resp.added_terms)):
        assert t in used, f"glossary term {t!r} was announced in the note but not searched"
    assert any(f"only the first {MAX_QUERY_TOKENS} distinct terms" in n for n in resp.notes)
    assert "distinct terms" in to_agent(resp)
    short_query = " ".join(f"zz{i}q" for i in range(20)) + " セーブデータ"
    short = searcher.search(short_query, mode="keyword", top_k=3)
    assert not any("distinct terms" in n for n in short.notes)
    assert searched[-1] == query_tokens(short_query + " " + " ".join(short.added_terms))     # 上限の下は今までどおり
