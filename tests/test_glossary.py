"""用語の対応表: 質問に、コードと文書で使われている語を足す。"""
from __future__ import annotations

import pytest

from context_foundation.config import ConfigError, load_config
from context_foundation.glossary import BUILTIN_MAX_ASCII_WORDS, Glossary, GlossaryError, parse
from context_foundation.search import Searcher

from conftest import write_config


def test_builtin_glossary_adds_english_words_to_a_japanese_question():
    g = Glossary.load(None, builtin=True)
    added = g.expand("GPU のバッファを確保して使い回す")
    assert added[:2] == ["buffer", "allocate"] and "reuse" in added and "pool" in added
    assert "gpu" not in [a.lower() for a in added]           # 質問にすでにある語は足さない


def test_builtin_glossary_is_skipped_when_the_question_already_has_english_words():
    g = Glossary.load(None, builtin=True)
    q = "GPU のバッファを使い回す buffer pool reuse"
    assert len([w for w in q.split() if w.isascii()]) - 1 >= BUILTIN_MAX_ASCII_WORDS
    assert g.expand(q) == []


def test_workspace_glossary_wins_and_matches_ascii_terms_on_word_boundaries(tmp_path):
    f = tmp_path / "glossary.tsv"
    f.write_text("# 注釈\nAudioMixer\tmixer bus volume\nバッファ\tgpubuf\n", encoding="utf-8")
    g = Glossary.load(f, builtin=True)
    assert g.expand("Unity の AudioMixer に当たるものは?")[:3] == ["mixer", "bus", "volume"]
    assert g.expand("MyAudioMixerX")[:1] != ["mixer"]           # 単語の切れ目でだけ当たる
    assert g.expand("バッファ")[0] == "gpubuf"                  # 同じ語なら、作業フォルダの対応表が先


def test_longer_terms_win_over_overlapping_shorter_ones():
    g = Glossary([("キー割り当", ["keybinding"]), ("キー", ["key"])])
    assert g.expand("キー割り当てはどこ") == ["keybinding"]


def test_parse_skips_malformed_lines_and_reports_them():
    problems: list[str] = []
    entries = parse("バッファ buffer\n確保\t\n描画\trender\n", "x.tsv", problems)
    assert entries == [("描画", ["render"])] and problems == ["x.tsv:1", "x.tsv:2"]


@pytest.mark.parametrize("query,unwanted", [
    ("ブロックの大きさを決める所", "lock"), ("キューブマップを作る所", "queue"), ("スキーマの検証", "key"),
    ("ダイアログを表示する", "log"), ("パスワードの確認", "path"), ("フレームワークの初期化", "frame"),
])
def test_katakana_terms_do_not_match_inside_other_katakana_words(query, unwanted):
    assert unwanted not in Glossary.load(None, builtin=True).expand(query)


def test_katakana_terms_still_match_on_their_own_and_with_a_long_vowel():
    g = Glossary.load(None, builtin=True)
    assert "lock" in g.expand("ロックを取る所") and "queue" in g.expand("キューに積む")
    assert "scheduler" in g.expand("スケジューラーの実装")


def test_half_width_katakana_is_normalized():
    assert "buffer" in Glossary.load(None, builtin=True).expand("ﾊﾞｯﾌｧを確保")


def test_names_paths_and_acronyms_do_not_count_as_english_words():
    g = Glossary.load(None, builtin=True)
    assert "allocate" in g.expand("std::vector<int> の再確保を避ける所")
    assert "initialize" in g.expand("src/render/buffer.cpp の初期化")
    assert "render" in g.expand("GPU と CPU と VRAM の描画")


def test_search_reports_the_added_terms_and_can_be_turned_off(tmp_path, corpus_copy):
    from context_foundation.indexer import build_index
    ws = tmp_path / "ws"
    build_index(load_config(write_config(ws, corpus_copy)), progress=False, log=lambda s: None)
    s = Searcher.from_config(load_config(ws / ".cf" / "config.toml"))
    r = s.search("バッファを使い回す処理", mode="keyword", top_k=3)
    assert "buffer" in r.added_terms and any("glossary" in n for n in r.notes)
    assert r.passages[0]["path"].startswith("Render/BufferPool")
    off = write_config(ws, corpus_copy, extra="[search]\nbuiltin_glossary = false\n")
    r = Searcher.from_config(load_config(off)).search("バッファを使い回す処理", mode="keyword", top_k=3)
    assert r.added_terms == []


def test_japanese_document_corpus_keeps_its_keyword_weight(tmp_path):
    """日本語の文書が主の索引: 足した英語の語が無くても、元の語で keyword の重みを決める。"""
    from context_foundation.indexer import build_index
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "design.md").write_text("# 排他制御の設計方針\n\n共有の資源は、排他制御で守る。\n", encoding="utf-8")
    cf = tmp_path / "ws" / ".cf"
    cf.mkdir(parents=True)
    (cf / "config.toml").write_text(f"[[source]]\nid = 'docs'\nroot = '{docs}'\n", encoding="utf-8")
    build_index(load_config(cf / "config.toml"), progress=False, log=lambda s: None)
    on = Searcher.from_config(load_config(cf / "config.toml")).search("排他制御の設計方針", mode="hybrid")
    (cf / "config.toml").write_text(f"[[source]]\nid = 'docs'\nroot = '{docs}'\n\n[search]\nbuiltin_glossary = false\n",
                                    encoding="utf-8")
    off = Searcher.from_config(load_config(cf / "config.toml")).search("排他制御の設計方針", mode="hybrid")
    assert on.added_terms and on.timings["keyword_weight"] >= off.timings["keyword_weight"]


def test_missing_workspace_glossary_file_is_a_config_error(tmp_path, corpus_copy):
    path = write_config(tmp_path / "ws", corpus_copy, extra="[search]\nglossary = 'nope.tsv'\n")
    with pytest.raises(ConfigError):
        Searcher.from_config(load_config(path))
