"""`ctxf sources`: 検索するフォルダ(設定の [[source]])を見る・替える。/context-index のダイアログが使う。"""
from __future__ import annotations

import json
import tomllib

import pytest

from context_foundation.cli import main as cli_main
from context_foundation.config import ConfigError, config_template, load_config, rewrite_sources


def _json(capsys, argv):
    capsys.readouterr()
    code = cli_main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return code, (json.loads(out[-1]) if out else None)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """docs・app・engine のある作業フォルダ(設定はまだ無い)。"""
    w = tmp_path / "ws"
    for name in ("docs", "app", "engine"):
        (w / name).mkdir(parents=True)
        (w / name / "a.md").write_text("# a\n", encoding="utf-8")
    monkeypatch.chdir(w)
    monkeypatch.delenv("CF_CONFIG", raising=False)
    return w


def test_sources_without_a_config_reports_an_empty_state(ws, capsys):
    code, state = _json(capsys, ["sources", "--json", "--workspace", str(ws)])
    assert code == 0
    assert state["config"] == "" and state["sources"] == [] and state["index"] is None
    assert state["workspace"] == str(ws) and state["embedding"]["enabled"] is True
    assert cli_main(["sources"]) == 3                       # 人が読む形では、設定が無いと知らせる
    assert cli_main(["sources", "--set", "docs"]) == 3
    assert cli_main(["sources", "--dry-run"]) == 2          # --dry-run は --set と一緒に


def test_sources_lists_the_folders_and_marks_nested_ones(ws, capsys):
    (ws / "docs" / "deep").mkdir()
    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[[source]]\nid = 'deep'\nroot = 'docs/deep'\n", encoding="utf-8")
    code, state = _json(capsys, ["sources", "--json"])
    assert code == 0 and state["config"] == str(cfg)
    got = {s["id"]: s for s in state["sources"]}
    assert got["docs"]["root"] == "docs" and got["docs"]["inside"] == "" and got["docs"]["exists"]
    assert got["deep"]["inside"] == "docs"                  # ほかの source の中にある
    assert state["index"] is None                           # 索引はまだ無い


def test_set_replaces_only_the_source_tables(ws, capsys):
    assert cli_main(["init", "docs", "engine"]) == 0
    cfg = ws / ".cf" / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    # 手で直した設定: docs に exclude を足し、[search] の前にコメントを置き、top_k を変える
    text = text.replace("root = 'docs'\n", "root = 'docs'\nexclude = ['**/old/**']   # mine\n")
    text = text.replace("[search]\n", "# 検索の決まり(手で書いた)\n[search]\n").replace("top_k = 8", "top_k = 5")
    cfg.write_text(text, encoding="utf-8")
    before = cfg.read_bytes()

    code, plan = _json(capsys, ["sources", "--set", "docs", "app", "--dry-run", "--json"])
    assert code == 0 and plan["result"] == "would change" and plan["backup"] == ""
    assert [s["id"] for s in plan["keep"]] == ["docs"] and [s["id"] for s in plan["remove"]] == ["engine"]
    assert plan["add"] == [{"id": "app", "root": "app"}]
    assert cfg.read_bytes() == before                       # 書いていない
    assert not (ws / ".cf" / "config.toml.bak").exists()

    code, done = _json(capsys, ["sources", "--set", "docs", "app", "--json"])
    assert code == 0 and done["result"] == "changed" and done["backup"].endswith("config.toml.bak")
    assert (ws / ".cf" / "config.toml.bak").read_bytes() == before
    after = cfg.read_text(encoding="utf-8")
    assert "exclude = ['**/old/**']   # mine" in after      # 残した source の中身はそのまま
    assert "# 検索の決まり(手で書いた)\n[search]" in after and "top_k = 5" in after
    assert "'engine'" not in after
    loaded = load_config(cfg)
    assert [s.id for s in loaded.sources] == ["docs", "app"] and loaded.sources[0].exclude == ["**/old/**"]
    assert loaded.search.top_k == 5
    assert after.index("id = 'app'") < after.index("[embedding]")   # 足した source は、source の並びの後ろ

    code, same = _json(capsys, ["sources", "--set", "app", "docs", "--json"])
    assert code == 0 and same["result"] == "unchanged" and same["backup"] == ""


def test_set_keeps_sources_inside_a_chosen_folder_and_checks_folders(ws, capsys):
    (ws / "docs" / "deep").mkdir()
    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[[source]]\nid = 'deep'\nroot = 'docs/deep'\n", encoding="utf-8")
    code, plan = _json(capsys, ["sources", "--set", "docs", "--dry-run", "--json"])
    assert code == 0 and [s["id"] for s in plan["keep"]] == ["docs", "deep"]      # 選んだフォルダの中の source は残す
    assert [s["id"] for s in plan["remove"]] == ["app"] and plan["add"] == []
    # 作業フォルダ全体を選ぶと、中の source はどれも残り、作業フォルダそのものを足す
    code, plan = _json(capsys, ["sources", "--set", ".", "--dry-run", "--json"])
    assert code == 0 and [s["id"] for s in plan["keep"]] == ["docs", "app", "deep"] and plan["remove"] == []
    assert [s["root"] for s in plan["add"]] == ["."]
    before = cfg.read_bytes()
    assert cli_main(["sources", "--set", "no-such-folder"]) == 2
    assert cfg.read_bytes() == before


def test_set_stops_when_the_file_cannot_be_split_by_lines(ws, capsys):
    """複数行の文字列の中に、表の見出しの形の行がある設定は、書き換えずに止まる(手で直すよう知らせる)。"""
    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    odd = cfg.read_text(encoding="utf-8").replace(
        "[embedding]\n", "[embedding]\nmodel_dir = '''\n[[source]]\n'''\n")
    cfg.write_text(odd, encoding="utf-8")
    tomllib.loads(odd)                                      # TOML としては読める
    before = cfg.read_bytes()
    capsys.readouterr()
    assert cli_main(["sources", "--set", "docs", "engine"]) == 2
    assert "手で直してください" in capsys.readouterr().err
    assert cfg.read_bytes() == before and not (ws / ".cf" / "config.toml.bak").exists()


def test_rewrite_sources_keeps_comments_of_the_next_table_and_line_endings():
    text = config_template([("a", "a"), ("b", "b")])
    text = text.replace("[embedding]", "# 次の表のコメント\n[embedding]")
    out = rewrite_sources(text, {"a"}, [("c", "c dir")])
    assert "id = 'b'" not in out and "# 次の表のコメント\n[embedding]" in out
    data = tomllib.loads(out)
    assert [s["id"] for s in data["source"]] == ["a", "c"] and data["source"][1]["root"] == "c dir"
    # 1 つも残さないときは、最初の source のあった場所に足す(始めのコメントの後ろ)
    out = rewrite_sources(text, set(), [("c", "c")])
    assert out.index("id = 'c'") < out.index("[embedding]") and out.startswith("# context-foundation の設定")
    # CRLF の設定は、CRLF のまま
    crlf = rewrite_sources(text.replace("\n", "\r\n"), {"b"}, [("c", "c")])
    assert "\r\n" in crlf and "\n" not in crlf.replace("\r\n", "")
    assert [s["id"] for s in tomllib.loads(crlf)["source"]] == ["b", "c"]
    with pytest.raises(ConfigError):
        rewrite_sources("[embedding]\nenabled = true\n", set(), [("c", "c")])
