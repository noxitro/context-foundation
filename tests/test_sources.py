"""`ctxf sources`: 検索するフォルダ(設定の [[source]])を見る・替える。/context-index のダイアログが使う。"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

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
    assert code == 0 and plan["result"] == "would change"
    assert plan["backup"] == str(ws / ".cf" / "config.toml.bak")      # 控えを置く場所(まだ書いていない)
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
    for argv in (["sources", "--set", "docs", "engine", "--dry-run"], ["sources", "--set", "docs", "engine"]):
        capsys.readouterr()
        assert cli_main(argv) == 2                          # 書く前の確かめ(--dry-run)でも止まる
        assert "手で直してください" in capsys.readouterr().err
        assert cfg.read_bytes() == before and not (ws / ".cf" / "config.toml.bak").exists()


def test_set_checks_the_result_before_writing(ws, capsys):
    """行で区切った結果が意図と違えば(見出しを引用符で書いた source は、区切りに掛からない)、書かずに止まる。"""
    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[['source']]\nid = 'engine'\nroot = 'engine'\n", encoding="utf-8")
    assert [s.id for s in load_config(cfg).sources] == ["docs", "app", "engine"]
    before = cfg.read_bytes()
    capsys.readouterr()
    assert cli_main(["sources", "--set", "docs", "app"]) == 2          # engine を外せない
    assert "手で直してください" in capsys.readouterr().err
    assert cfg.read_bytes() == before and not (ws / ".cf" / "config.toml.bak").exists()


def test_set_restores_the_config_when_the_written_one_cannot_be_loaded(ws, capsys, monkeypatch):
    import context_foundation.cli as cli

    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    before = cfg.read_bytes()
    real, calls = cli.load_config, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 2:                                 # 書いた後の読み直しだけ、読めないことにする
            raise ConfigError("読めません")
        return real(path)

    monkeypatch.setattr(cli, "load_config", flaky)
    assert cli_main(["sources", "--set", "docs"]) == 2
    assert cfg.read_bytes() == before                       # 元に戻している
    assert (ws / ".cf" / "config.toml.bak").read_bytes() == before


def test_set_names_the_backup_after_the_config_file(ws, capsys):
    """CF_CONFIG や --config で、別の名前の設定を使っているとき: 控えは、その名前に .bak(隣の別の控えを上書きしない)。"""
    assert cli_main(["init", "docs", "app"]) == 0
    mine = ws / ".cf" / "mine.toml"
    mine.write_bytes((ws / ".cf" / "config.toml").read_bytes())
    other = ws / ".cf" / "config.toml.bak"
    other.write_text("# 別の控え\n", encoding="utf-8")
    code, done = _json(capsys, ["sources", "--config", str(mine), "--set", "docs", "--json"])
    assert code == 0 and done["result"] == "changed" and done["backup"] == str(ws / ".cf" / "mine.toml.bak")
    assert (ws / ".cf" / "mine.toml.bak").is_file() and other.read_text(encoding="utf-8") == "# 別の控え\n"


def test_a_config_saved_with_a_bom_is_read_and_keeps_its_bom(ws, capsys):
    """Windows のメモ帳や PowerShell 5.1 は、UTF-8 で保存すると先頭に BOM を付ける。読み飛ばして読み、書き直しても残す。"""
    bom = b"\xef\xbb\xbf"
    assert cli_main(["init", "docs", "app"]) == 0
    cfg = ws / ".cf" / "config.toml"
    cfg.write_bytes(bom + cfg.read_bytes())
    assert [s.id for s in load_config(cfg).sources] == ["docs", "app"]
    code, state = _json(capsys, ["sources", "--json"])
    assert code == 0 and [s["id"] for s in state["sources"]] == ["docs", "app"]
    code, done = _json(capsys, ["sources", "--set", "docs", "engine", "--json"])
    assert code == 0 and done["result"] == "changed"
    assert cfg.read_bytes().startswith(bom) and not cfg.read_bytes()[3:].startswith(bom)
    assert [s.id for s in load_config(cfg).sources] == ["docs", "engine"]
    # UTF-8 でない設定(UTF-16 など)は、これまでどおり断る
    cfg.write_bytes("[[source]]\nid = 'a'\nroot = 'a'\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8 ではありません"):
        load_config(cfg)


def test_folders_whose_names_start_with_a_dash_are_given_with_a_dot_slash(ws, capsys):
    """- で始まるフォルダは、オプションと読まれないように ./ を付けて渡す(/context-index の拡張は、そうして渡す)。"""
    (ws / "-old").mkdir()
    (ws / "-old" / "a.md").write_text("# a\n", encoding="utf-8")
    assert cli_main(["init", "docs", "./-old"]) == 0
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [s.root for s in cfg.sources] == [ws / "docs", ws / "-old"]
    code, state = _json(capsys, ["sources", "--json"])
    assert [s["root"] for s in state["sources"]] == ["docs", "-old"]
    code, plan = _json(capsys, ["sources", "--set", "./-old", "app", "--dry-run", "--json"])
    assert code == 0 and [s["root"] for s in plan["keep"]] == ["-old"] and [s["root"] for s in plan["remove"]] == ["docs"]
    assert plan["add"] == [{"id": "app", "root": "app"}]


def test_set_keeps_a_source_rooted_at_a_link_when_its_target_is_chosen(ws, tmp_path, capsys):
    """ジャンクションを root にした source を、その先の絶対パスで選んでも、外して足し直さない(exclude を失わない)。"""
    target = tmp_path / "elsewhere" / "Engine"
    target.mkdir(parents=True)
    (ws / "external").mkdir()
    link = ws / "external" / "Engine"
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"ここではフォルダのリンクを作れない: {e}")
    assert cli_main(["init", "docs", str(Path("external") / "Engine")]) == 0
    code, plan = _json(capsys, ["sources", "--set", "docs", str(target), "--dry-run", "--json"])
    assert code == 0 and plan["result"] == "unchanged", plan
    assert [s["root"] for s in plan["keep"]] == ["docs", os.path.join("external", "Engine")]


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


def test_rewrite_sources_decides_which_comments_go_with_a_removed_source():
    def src(sid):
        return f"[[source]]\nid = '{sid}'\nroot = '{sid}'\n"

    # 空行の後ろのコメント(その後ろに空行があっても)は、次の表のもの。source を外しても残る
    text = src("a") + "\n" + src("b") + "\n# ---- 埋め込みの設定 ----\n\n[embedding]\nenabled = true\n"
    out = rewrite_sources(text, {"a"}, [])
    assert "id = 'b'" not in out and "# ---- 埋め込みの設定 ----\n\n[embedding]" in out
    # ファイルの終わりの、空行の後ろのコメントも残る
    out = rewrite_sources(src("a") + "\n" + src("b") + "\n# メモ\n", {"a"}, [])
    assert "id = 'b'" not in out and out.endswith("# メモ\n")
    # 外す source の見出しのすぐ上のコメントは、一緒に外す。残す source のものは、そのまま
    text = "# 始めのコメント\n" + src("a") + "\n# b について\n" + src("b") + "\n# c について\n" + src("c")
    out = rewrite_sources(text, {"a", "c"}, [])
    assert "# b について" not in out and "# c について\n[[source]]\nid = 'c'" in out
    assert out.startswith("# 始めのコメント\n")
    # ファイルの始めのコメントは、すぐ下の source を外しても残す
    assert rewrite_sources(text, {"b", "c"}, []).startswith("# 始めのコメント\n")
    # 空行を挟まずに続くコメントは、その source のもの(外すと一緒に消える)
    out = rewrite_sources(src("a") + "\n" + src("b") + "# exclude = ['x']\n\n[search]\ntop_k = 5\n", {"a"}, [])
    assert "# exclude" not in out and "[search]\ntop_k = 5" in out
