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


def test_parse_rejects_lines_without_a_tab():
    with pytest.raises(GlossaryError):
        parse("バッファ buffer\n", "x.tsv")


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


def test_missing_workspace_glossary_file_is_a_config_error(tmp_path, corpus_copy):
    path = write_config(tmp_path / "ws", corpus_copy, extra="[search]\nglossary = 'nope.tsv'\n")
    with pytest.raises(ConfigError):
        Searcher.from_config(load_config(path))
