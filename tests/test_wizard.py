""".bat から呼ぶ対話の形(索引づくり・検索)。入力は、キーボードの代わりに並びで渡す。"""
from __future__ import annotations

import builtins
import shutil
from argparse import Namespace

import pytest

from context_foundation.config import load_config
from context_foundation.search import Searcher
from context_foundation.wizard import main as wizard_main

from conftest import CORPUS


@pytest.fixture
def answers(monkeypatch, tmp_path):
    """input() に、用意した答えを順に返させる。状態ファイルとモデルの置き場は、一時フォルダに向ける。"""
    # 覚え書きの置き場は、OS によって決め方が違う。CF_HOME で、どの OS でも一時フォルダに向ける
    monkeypatch.setenv("CF_HOME", str(tmp_path / "cf-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    queue: list[str] = []

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return queue


def test_index_wizard_writes_config_and_builds_the_index(answers, tmp_path, capsys):
    ws = tmp_path / "work"
    shutil.copytree(CORPUS / "engine", ws / "engine")
    outside = tmp_path / "elsewhere" / "design docs"
    shutil.copytree(CORPUS / "docs", outside)
    answers += [
        str(ws),                 # 作業フォルダ
        str(ws / "engine"), "",  # 検索したいフォルダ 1 と、その呼び名(既定のまま)
        str(outside), "docs",    # 作業フォルダの外のフォルダと、その呼び名
        "",                      # 終わり
        "n",                     # 埋め込みモデルは取得しない → keyword だけ
    ]
    assert wizard_main(Namespace(action="index")) == 0
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [(s.id, s.root) for s in cfg.sources] == [("engine", ws / "engine"), ("docs", outside)]
    text = (ws / ".cf" / "config.toml").read_text(encoding="utf-8")
    assert "root = 'engine'" in text                   # 作業フォルダの中は、相対パスで書く
    assert (ws / ".cf" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    s = Searcher.from_config(cfg)
    assert s.status()["modes"] == ["keyword"] and s.status()["stats"]["files"] == 11
    out = capsys.readouterr().out
    assert "keyword(BM25)だけの検索になります" in out and "設定を書きました" in out

    # 2 回目は、設定があるので、作り直すかだけを尋ねる
    answers += ["", "n"]
    assert wizard_main(Namespace(action="index")) == 0
    assert "やめました" in capsys.readouterr().out


def test_search_wizard_loops_until_an_empty_line(answers, indexed_workspace, capsys, monkeypatch):
    monkeypatch.setenv("CF_TEST_EMBEDDER", "hash")
    answers += [str(indexed_workspace), ":keyword", "job scheduler steal", ""]
    assert wizard_main(Namespace(action="search")) == 0
    out = capsys.readouterr().out
    assert "方法を keyword にしました" in out and "engine:Core/JobScheduler.cpp:" in out


def test_wizard_stops_when_input_is_closed(answers):
    with pytest.raises(SystemExit):
        wizard_main(Namespace(action="index"))


def test_closed_input_never_downloads_the_model(answers, tmp_path, monkeypatch, capsys):
    """入力が閉じられていたら、モデルを取得しない(尋ねずに数百 MB を取得しない)。keyword だけで索引を作る。"""
    import context_foundation.embed as embed
    from context_foundation.wizard import _save_state

    from conftest import write_config

    ws = tmp_path / "work"
    ws.mkdir()
    write_config(ws)
    _save_state(workspace=str(ws))       # 前回の作業フォルダを覚えている(尋ねられずに進む)
    called = []
    monkeypatch.setattr(embed, "download_model", lambda *a, **k: called.append(a))
    assert wizard_main(Namespace(action="index")) == 0
    out = capsys.readouterr().out
    assert not called and "取得しませんでした" in out
    assert Searcher.from_config(load_config(ws / ".cf" / "config.toml")).status()["modes"] == ["keyword"]
