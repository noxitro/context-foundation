"""プラグインとしての形: 定義ファイル、入口(ctxf.py)の python の探し方と用意の知らせ、init コマンド。

Copilot に実際に入れる確認は、手元で設定を分けて行う(docs/copilot.md の「確かめた範囲」)。ここでは、
入れる前に壊れていないことと、入口が利用者ごとの置き場の仮想環境を正しく扱うことを確かめる。
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from context_foundation import __version__
from context_foundation.cli import main as cli_main
from context_foundation.config import load_config

REPO = Path(__file__).resolve().parents[1]


def load_launcher(monkeypatch, cf_home: Path):
    monkeypatch.setenv("CF_HOME", str(cf_home))
    monkeypatch.delenv("CF_PYTHON", raising=False)
    spec = importlib.util.spec_from_file_location("ctxf_under_test", REPO / "ctxf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_versions_agree_and_changelog_has_the_current_version():
    # 版は 4 か所でそろえ、変更履歴の一番上の見出しが今の版になっている(版を上げたら、変更履歴も書く)
    plugin = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))
    (entry,) = json.loads((REPO / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"]
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert plugin["version"] == entry["version"] == project["version"] == __version__
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## (\d+\.\d+\.\d+)", changelog, re.M)
    assert versions and versions[0] == __version__
    latest = changelog.split("\n## ")[1]
    assert "### 更新のあとにすること" in latest
    # チームの Marketplace に載せる例の版も、今の版
    assert f'"version": "{__version__}"' in (REPO / "docs" / "copilot.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    return text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


def test_reporting_sections_ask_to_mask_the_paths_of_the_where_output():
    # where の出力は、利用者の名前を含む絶対パスを出す。issue は公開されるので、どちらの README も伏せるよう求める
    en = _section((REPO / "README.md").read_text(encoding="utf-8"), "Reporting problems")
    ja = _section((REPO / "readme-ja.md").read_text(encoding="utf-8"), "不具合の報告")
    assert "where" in en and "mask" in en
    assert "where" in ja and "伏せ" in ja


def test_documented_index_exit_codes_match_the_code():
    # 無人で流す index の終了コードの説明に、コードが返す値がすべてある(用意ができていない 4 を含む)
    from context_foundation import cli
    import re
    codes = {0, 1, cli.EXIT_USAGE, cli.EXIT_NO_INDEX, 4, 130}
    for name, heading in (("readme-ja.md", "コマンド"), ("docs/copilot.md", "索引を更新する")):
        part = _section((REPO / name).read_text(encoding="utf-8"), heading)
        para = part[part.index("終了コードは"):].split("\n\n", 1)[0]
        # 「130 が」の中の「0 が」に当てない(0 の説明が消えたら落ちる)。書いてある値と、コードの値が同じ
        assert {int(c) for c in re.findall(r"(?<!\d)(\d+) が", para)} == codes, name


def test_copilot_doc_names_the_default_surface():
    # アプリでの確かめ方は、既定の出し方(MCP の道具)を見るように書いてある
    doc = (REPO / "docs" / "copilot.md").read_text(encoding="utf-8")
    app = doc.split("\n### アプリ\n", 1)[1].split("\n### ", 1)[0]
    assert "context-foundation-search" in app


def test_documented_storage_paths_match_the_code(monkeypatch, tmp_path):
    # 「保存されるものと消し方」の表に、コードが使う置き場の名前が出ている
    from context_foundation import embed, wizard
    launcher = load_launcher(monkeypatch, tmp_path / "home")
    doc = (REPO / "docs" / "copilot.md").read_text(encoding="utf-8")
    names = [Path(launcher.runtime_dir()).name, Path(launcher.runtime_dir()).name + "\\venv",
             Path(launcher._stamp_path()).name, embed.default_cache_dir().name, wizard._state_file().name,
             ".local/share/context-foundation", ".cache/context-foundation", "index.db.partial", "index.db.new"]
    assert [n for n in names if n not in doc] == []


def test_plugin_manifest_marketplace_and_hook_agree():
    plugin = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((REPO / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
    hooks = json.loads((REPO / "com.github.copilot" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] == "context-foundation" and entry["source"] == "./"
    assert plugin["version"] == entry["version"] == project["version"] == __version__   # 版をそろえる
    (hook,) = hooks["hooks"]["sessionStart"]
    assert hook["command"] == 'python -I "${PLUGIN_ROOT}/ctxf.py" session-hint --mcp' and hook["timeoutSec"] <= 10
    # Agent Plugins 1.0: MCP サーバーの定義は、ルートの mcp.json(場所は固定)。入口 ctxf.py を通して起動する
    mcp = json.loads((REPO / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    ((name, server),) = mcp["mcpServers"].items()
    assert name == "context-foundation" and server["type"] == "stdio" and server["command"] == "python"
    assert server["args"] == ["-I", "${PLUGIN_ROOT}/ctxf.py", "serve"]
    # Agent Plugins 1.0: skill は skills/ の下だけから読まれる
    assert (REPO / "skills" / "context-search" / "SKILL.md").is_file()


def test_extension_adds_the_index_command_and_its_library_passes_its_tests():
    # 索引を作るスラッシュコマンド /context-index(モデルを呼ばない拡張)。Agent Plugins 1.0 では
    # com.github.copilot/extensions/ の下に置くと読まれる(cxxref で確かめた形。docs/copilot.md の「/context-index」)
    folder = REPO / "com.github.copilot" / "extensions" / "context-foundation"
    ext = (folder / "extension.mjs").read_text(encoding="utf-8")
    assert 'name: "context-index"' in ext and 'name: "context-setup"' in ext
    # 入口は、プラグインのルートの ctxf.py(skill のフォルダの写しは要らない)。/context-index は init と index だけを
    # 動かし、setup は /context-setup だけが動かす(利用者が打つコマンド。大きな取得を、索引のコマンドからは始めない)
    assert ext.count('"ctxf.py")') == 2 and '"init"' in ext and '"index"' in ext
    assert ext.count('"setup"') == 1 and '[ctxf, "setup", ...opts.args]' in ext
    # /context-setup は、手元に置いたモデルのフォルダを model use で覚えるだけ。モデルを取得するコマンドは動かさない
    assert ext.count('"model", "use"') == 2 and '"download"' not in ext and '"--download"' not in ext
    plugin = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))
    assert "/context-index" in plugin["description"] and "/context-setup" in plugin["description"]
    # skill は、索引が無いとき利用者にこのコマンドを勧める
    assert "/context-index" in (REPO / "skills" / "context-search" / "SKILL.md").read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    r = subprocess.run([node, "--check", str(folder / "extension.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    # SDK に依らない部分(プラグインの場所・作業フォルダ・引数・進み具合・失敗の文)の単体テスト
    r = subprocess.run([node, "--test", str(folder / "lib.test.mjs")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_extension_skips_the_same_folder_names_as_ingest():
    # /context-index のフォルダを選ぶ一覧は、索引に入らない名前(ingest.py の DEFAULT_EXCLUDED_DIRS)を出さない。
    # 拡張は Python を読み込めないので、同じ一覧を手で写している。ずれたら落とす
    from context_foundation.ingest import DEFAULT_EXCLUDED_DIRS
    lib = (REPO / "com.github.copilot" / "extensions" / "context-foundation" / "lib.mjs").read_text(encoding="utf-8")
    body = re.search(r"export const SKIP_DIRS = new Set\(\[(.*?)\]\);", lib, re.S)
    assert body, "lib.mjs に SKIP_DIRS が無い"
    assert set(re.findall(r'"([^"]+)"', body.group(1))) == DEFAULT_EXCLUDED_DIRS


def test_launcher_without_a_runtime_says_how_to_set_up(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "HERE", str(tmp_path / "plugin"))     # 隣に .venv の無い、入れたばかりのプラグイン
    py, _, reason = ctxf.find_python()
    assert py is None and reason == "not set up"
    msg = ctxf.not_ready_message(reason)
    assert "ask the user" in msg and "ctxf.py\" setup" in msg and "grep" in msg


def test_launcher_uses_the_runtime_only_while_its_dependencies_match(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ctxf = load_launcher(monkeypatch, home)
    monkeypatch.setattr(ctxf, "HERE", str(tmp_path / "plugin"))
    venv_py = home / "runtime" / "venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    stamp = home / "runtime" / "deps.json"
    stamp.write_text(json.dumps({"deps_hash": ctxf.deps_hash(ctxf.dependencies())}), encoding="utf-8")
    py, kind, _ = ctxf.find_python()
    assert py == str(venv_py) and kind == "runtime"
    stamp.write_text(json.dumps({"deps_hash": "older"}), encoding="utf-8")      # プラグインの更新で依存が変わった
    py, _, reason = ctxf.find_python()
    assert py is None and "dependencies changed" in reason


def test_launcher_without_a_finished_setup_says_so(monkeypatch, tmp_path):
    """仮想環境はあるが、setup が最後まで終わっていない(覚え書きが無い)とき。"""
    home = tmp_path / "home"
    ctxf = load_launcher(monkeypatch, home)
    monkeypatch.setattr(ctxf, "HERE", str(tmp_path / "plugin"))
    venv_py = home / "runtime" / "venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    py, _, reason = ctxf.find_python()
    assert py is None and "did not finish" in reason


def test_setup_checks_its_arguments_before_downloading_anything(tmp_path):
    env = {"CF_HOME": str(tmp_path / "home"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "setup", "--help"], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("usage:") and "Ask the user" in r.stdout
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "setup", "--dry-run"], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 2 and "unknown option" in r.stdout
    assert not (tmp_path / "home" / "runtime" / "venv").exists()      # 何も作っていない


def test_launcher_prefers_cf_python_then_the_local_venv(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    plugin = tmp_path / "plugin"
    local = plugin / ".venv" / "Scripts" / "python.exe"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"")
    monkeypatch.setattr(ctxf, "HERE", str(plugin))
    assert ctxf.find_python()[:2] == (str(local), "local .venv")
    monkeypatch.setenv("CF_PYTHON", "C:/somewhere/python.exe")
    assert ctxf.find_python()[:2] == ("C:/somewhere/python.exe", "CF_PYTHON")


def test_session_hint_says_not_ready_when_the_tool_is_not_set_up(indexed_workspace, tmp_path):
    """索引はあるが、この PC に用意が無いとき: skill を読めば用意の仕方が分かる、と知らせる。"""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "ctxf.py").write_bytes((REPO / "ctxf.py").read_bytes())       # 隣に .venv の無い写し
    (plugin / "pyproject.toml").write_bytes((REPO / "pyproject.toml").read_bytes())
    env = {"CF_HOME": str(tmp_path / "empty-home"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    r = subprocess.run([sys.executable, "-I", str(plugin / "ctxf.py"), "session-hint"], capture_output=True,
                       input=json.dumps({"cwd": str(indexed_workspace)}), text=True, encoding="utf-8", env=env,
                       timeout=60)
    assert r.returncode == 0
    ctx = json.loads(r.stdout)["additionalContext"]
    assert "not set up" in ctx and "context-search skill" in ctx
    r = subprocess.run([sys.executable, "-I", str(plugin / "ctxf.py"), "search", "anything"], capture_output=True,
                       text=True, encoding="utf-8", env=env, cwd=indexed_workspace, timeout=60)
    assert r.returncode == 4 and "not set up" in r.stdout and "setup" in r.stdout


def test_init_writes_a_config_with_relative_and_absolute_roots(tmp_path, corpus_copy, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    monkeypatch.chdir(ws)
    assert cli_main(["init", "docs", str(corpus_copy / "engine")]) == 0
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [s.id for s in cfg.sources] == ["docs", "engine"]
    assert cfg.sources[0].root == ws / "docs" and cfg.sources[1].root == corpus_copy / "engine"
    assert (ws / ".cf" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert cli_main(["init", "docs"]) == 2                     # すでにあれば、--force なしでは書き直さない
    (ws / ".cf" / ".gitignore").write_text("*\n# mine\n", encoding="utf-8")
    assert cli_main(["init", "docs", "--force"]) == 0
    assert (ws / ".cf" / ".gitignore").read_text(encoding="utf-8") == "*\n# mine\n"   # あれば書き換えない
    assert (ws / ".cf" / "config.toml.bak").is_file()                                  # 前の設定を残す
    assert cli_main(["init", str(tmp_path / "no-such-folder"), "--force"]) == 2


def _dir_link(target: Path, link: Path) -> None:
    """フォルダのリンク(Windows ではジャンクション。Linux・macOS ではシンボリックリンクで代える)。"""
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"ここではフォルダのリンクを作れない: {e}")


def test_init_keeps_a_linked_folder_under_the_workspace_as_written(tmp_path, corpus_copy, monkeypatch):
    """external の下のジャンクションは、解決せずに 'external/Engine' と書き、呼び名もリンクの名前から作る。
    作業フォルダ('.')と並べても、リンクの先のファイルは Engine の側だけに 1 回入る。"""
    from context_foundation.indexer import build_index
    from context_foundation.search import Searcher

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Main.cpp").write_text("int app_main_entry();\n", encoding="utf-8")
    (ws / "external").mkdir()
    _dir_link(corpus_copy / "engine", ws / "external" / "Engine")
    monkeypatch.chdir(ws)
    assert cli_main(["init", ".", str(Path("external") / "Engine")]) == 0
    text = (ws / ".cf" / "config.toml").read_text(encoding="utf-8")
    assert f"root = '{Path('external') / 'Engine'}'" in text
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [(s.id, s.root) for s in cfg.sources] == [("ws", ws), ("Engine", ws / "external" / "Engine")]
    cfg.embedding.enabled = False
    build_index(cfg, progress=False, log=lambda s: None)
    st = Searcher.from_config(cfg).status()
    assert st["stats"]["nested_roots"] == ["ws:external/Engine → Engine"]
    assert "skipped_dirs" not in st["stats"]
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(str(cfg.index_path))) as db:
        rows = list(db.execute("SELECT source_id, path FROM files"))
    assert ("ws", "src/Main.cpp") in rows
    assert not [p for sid, p in rows if sid == "ws" and p.startswith("external/")]
    assert ("Engine", "Render/BufferPool.cpp") in rows                 # リンクの先は Engine の側だけに入る


def _indexed(cfg) -> set[tuple[str, str]]:
    import sqlite3
    from contextlib import closing

    from context_foundation.indexer import build_index
    cfg.embedding.enabled = False
    build_index(cfg, progress=False, log=lambda s: None)
    with closing(sqlite3.connect(str(cfg.index_path))) as db:
        return set(db.execute("SELECT source_id, path FROM files"))


@pytest.mark.parametrize("given", [".", "external"])
def test_init_adds_a_source_for_each_linked_folder_inside_the_given_ones(tmp_path, corpus_copy, monkeypatch,
                                                                        capsys, given):
    """/context-index で作業フォルダ全体や external を選んでも、中のジャンクションの先が入る(0.4.15 から)。"""
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Main.cpp").write_text("int app_main_entry();\n", encoding="utf-8")
    (ws / "external").mkdir()
    _dir_link(corpus_copy / "engine", ws / "external" / "Engine")
    _dir_link(corpus_copy / "docs", ws / "external" / "Docs")
    monkeypatch.chdir(ws)
    assert cli_main(["init", given]) == 0
    out = capsys.readouterr().out
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [s.id for s in cfg.sources] == [ws.name if given == "." else "external", "Docs", "Engine"]
    assert cfg.sources[2].root == ws / "external" / "Engine"
    assert f"  source Engine: {Path('external') / 'Engine'}\n" in out and "2 個を" in out
    rows = _indexed(cfg)
    assert ("Engine", "Render/BufferPool.cpp") in rows and ("Docs", "jobs.md") in rows
    assert not [p for sid, p in rows if sid not in ("Docs", "Engine") and "BufferPool" in p]


def test_init_adds_only_linked_folders_that_point_somewhere_new(tmp_path, corpus_copy, monkeypatch):
    """中を指すもの・上のフォルダを指すもの・同じ先を指す 2 本目・足すリンクの先の中を指すもの・除く名前の
    フォルダの中と、その名前のリンク・3 段より下・壊れたリンクは足さない。"""
    ws = tmp_path / "ws"
    for d in ("src", "build", "a/b", "external"):
        (ws / d).mkdir(parents=True)
    _dir_link(corpus_copy / "engine", ws / "external" / "Engine")
    _dir_link(corpus_copy / "engine", ws / "external" / "Engine2")         # 同じ先の 2 本目
    _dir_link(ws / "src", ws / "external" / "Self")                         # 作業フォルダの中
    _dir_link(tmp_path, ws / "external" / "Up")                             # 作業フォルダを含む
    _dir_link(corpus_copy / "docs", ws / "build" / "Docs")                  # 除く名前のフォルダの中
    _dir_link(corpus_copy / "app", ws / "a" / "b" / "App")                  # 3 段目
    _dir_link(corpus_copy / "engine" / "Render", ws / "external" / "Render")  # 足すリンクの先の中
    _dir_link(corpus_copy / "docs", ws / "external" / "packages")           # 除く名前のリンク
    (tmp_path / "gone").mkdir()
    _dir_link(tmp_path / "gone", ws / "external" / "Broken")                # 壊れたリンク(先を後で消す)
    (tmp_path / "gone").rmdir()
    monkeypatch.chdir(ws)
    assert cli_main(["init", "."]) == 0
    cfg = load_config(ws / ".cf" / "config.toml")
    assert [s.id for s in cfg.sources] == [ws.name, "Engine"]


def test_source_root_is_relative_inside_the_workspace_and_absolute_outside(tmp_path):
    from context_foundation.config import source_root
    ws = tmp_path / "ws"
    (ws / "a" / "b").mkdir(parents=True)
    (tmp_path / "..foo").mkdir()
    (ws / "..foo").mkdir()
    assert source_root(ws, ws) == "."
    assert source_root(ws / "a" / "b", ws) == str(Path("a") / "b")
    assert source_root(ws / "..foo", ws) == "..foo"                   # 名前が .. で始まるだけなら中
    assert source_root(tmp_path / "..foo", ws) == str(tmp_path / "..foo")
    assert source_root(tmp_path, ws) == str(tmp_path)
    # 作業フォルダそのものをリンク越しに渡したとき: 字面では外でも、解決すると中なら、相対パスにする
    _dir_link(ws, tmp_path / "ws-link")
    assert source_root(ws / "a", tmp_path / "ws-link") == "a"


def test_register_status_reads_the_marketplace_name():
    from context_foundation.copilot_reg import PLUGIN_NAME, marketplace_name
    assert marketplace_name() == PLUGIN_NAME == "context-foundation"


# Copilot CLI 1.0.112 の `copilot plugin list --json` と `copilot plugin marketplace list --json` の実際の出力の形
# (パスだけ置き換えたもの)
PLUGIN_LIST = """[
  {"name": "context-foundation", "marketplace": "context-foundation", "version": "0.2.0", "enabled": true,
   "source": "live", "installedFrom": "%s"},
  {"name": "computer-use", "marketplace": "", "version": "0.1.97", "enabled": false, "source": "builtin"}
]"""
MARKET_LIST = """[
  {"name": "context-foundation", "source": "Local: %s", "isDefault": false},
  {"name": "awesome-copilot", "source": "GitHub: github/awesome-copilot", "isDefault": true}
]"""


def test_json_list_is_read_from_stdout_with_noise_around_it():
    from context_foundation.copilot_reg import StateUnreadable, parse_json_list
    noisy = "\x1b[33m[warn]\x1b[0m update available\n" + PLUGIN_LIST % "C:/x" + "\ntrailing notice\n"
    rows = parse_json_list(noisy)
    assert [r["name"] for r in rows] == ["context-foundation", "computer-use"]
    with pytest.raises(StateUnreadable):
        parse_json_list("no json here")


def test_plugin_and_marketplace_count_as_ours_only_when_they_point_here():
    from context_foundation.copilot_reg import (REPO_ROOT, find_marketplace, find_plugin, parse_json_list,
                                                plugin_is_ours)
    here = REPO_ROOT.as_posix()
    p = find_plugin(parse_json_list(PLUGIN_LIST % here))
    assert plugin_is_ours(p, "context-foundation")
    other = find_plugin(parse_json_list(PLUGIN_LIST % "C:/somewhere/else"))
    assert not plugin_is_ours(other, "context-foundation")
    m, ours = find_marketplace(parse_json_list(MARKET_LIST % here), "context-foundation")
    assert m is not None and ours
    m, ours = find_marketplace(parse_json_list(MARKET_LIST % "D:/old/clone"), "context-foundation")
    assert m is not None and not ours
    github = '[{"name": "context-foundation", "source": "GitHub: noxitro/context-foundation", "isDefault": false}]'
    m, ours = find_marketplace(parse_json_list(github), "context-foundation")
    assert m is not None and not ours


def test_register_stops_without_touching_a_marketplace_from_elsewhere(monkeypatch, capsys):
    """同じ名前の Marketplace が GitHub から登録されていれば、追加も削除もせずに止まる。"""
    from types import SimpleNamespace

    from context_foundation import copilot_reg
    calls = []

    def fake_run(exe, *args):
        calls.append(args)
        if args[:2] == ("plugin", "list"):
            return 0, "[]", ""
        if args[:3] == ("plugin", "marketplace", "list"):
            return 0, '[{"name": "context-foundation", "source": "GitHub: noxitro/context-foundation"}]', ""
        if args[:2] == ("skill", "list"):
            return 0, "[]", ""
        return 0, "", ""

    monkeypatch.setattr(copilot_reg, "_run", fake_run)
    monkeypatch.setattr(copilot_reg, "find_copilot", lambda: "copilot.exe")
    rc = copilot_reg.main(SimpleNamespace(action="register", mcp=False, yes=True))
    assert rc == 1 and "別の場所" in capsys.readouterr().err
    assert not any(a[:3] in (("plugin", "marketplace", "add"), ("plugin", "marketplace", "remove")) for a in calls)


def test_mcp_server_stays_up_without_tools_when_the_tool_is_not_set_up(tmp_path):
    """用意(setup)の前に MCP サーバーとして起動されても、落ちずに挨拶に答え、道具は 1 つも出さない。"""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "ctxf.py").write_bytes((REPO / "ctxf.py").read_bytes())       # 隣に .venv の無い写し
    (plugin / "pyproject.toml").write_bytes((REPO / "pyproject.toml").read_bytes())
    env = {"CF_HOME": str(tmp_path / "empty-home"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {}}}]
    r = subprocess.run([sys.executable, "-I", str(plugin / "ctxf.py"), "serve"], capture_output=True,
                       input="".join(json.dumps(m) + "\n" for m in lines) + "not json" + "\n", text=True,
                       encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0
    # 理由と用意のしかたは、標準エラー(ホストの MCP のログ)に 1 行。標準出力は JSON-RPC の行だけ
    assert "not set up" in r.stderr and "ctxf.py\" setup" in r.stderr and "new session" in r.stderr
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert [m["id"] for m in replies] == [1, 2, 3]                 # 通知と、壊れた行には答えない
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["serverInfo"]["name"] == "context-foundation"
    assert replies[1]["result"] == {"tools": []}
    message = replies[2]["error"]["message"]
    assert "not set up" in message and "ctxf.py\" setup" in message and "new session" in message
