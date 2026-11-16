"""プラグインとしての形: 定義ファイル、入口(ctxf.py)の python の探し方と用意の知らせ、init コマンド。

Copilot に実際に入れる確認は、手元で設定を分けて行う(docs/copilot.md の「確かめた範囲」)。ここでは、
入れる前に壊れていないことと、入口が利用者ごとの置き場の仮想環境を正しく扱うことを確かめる。
"""
from __future__ import annotations

import importlib.util
import json
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
    assert hook["command"] == 'python -I "${PLUGIN_ROOT}/ctxf.py" session-hint' and hook["timeoutSec"] <= 10
    # Agent Plugins 1.0: skill は skills/ の下だけから読まれる
    assert (REPO / "skills" / "context-search" / "SKILL.md").is_file()


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
    assert cfg.sources[0].root == (ws / "docs").resolve() and cfg.sources[1].root == (corpus_copy / "engine").resolve()
    assert (ws / ".cf" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert cli_main(["init", "docs"]) == 2                     # すでにあれば、--force なしでは書き直さない
    assert cli_main(["init", "docs", "--force"]) == 0
    assert cli_main(["init", str(tmp_path / "no-such-folder"), "--force"]) == 2


def test_register_status_reads_the_marketplace_name():
    from context_foundation.copilot_reg import PLUGIN_NAME, marketplace_name
    assert marketplace_name() == PLUGIN_NAME == "context-foundation"


@pytest.mark.parametrize("rows,expect", [("[]", None), ('[{"name": "context-foundation", "enabled": true}]', True)])
def test_installed_plugin_parses_the_json_list(monkeypatch, rows, expect):
    from context_foundation import copilot_reg
    monkeypatch.setattr(copilot_reg, "_run", lambda exe, *a: (0, "Plugins:\n" + rows))
    p = copilot_reg.installed_plugin("copilot")
    assert (p is None) if expect is None else p["enabled"] is True
