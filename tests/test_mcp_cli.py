"""MCP サーバー(stdio の JSON-RPC)と、コマンドラインを、別プロセスで起動して通しで確かめる。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from context_foundation.render import HARD_MAX_BYTES

from conftest import CASES, write_config

REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, CF_TEST_EMBEDDER="hash", PYTHONIOENCODING="utf-8")
ENV.pop("CF_CONFIG", None)


def run_cli(*args, cwd, env=ENV):
    return subprocess.run([sys.executable, "-m", "context_foundation", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)


class Mcp:
    def __init__(self, cwd, *args):
        self.p = subprocess.Popen([sys.executable, "-m", "context_foundation", "serve", *args], cwd=cwd, env=ENV,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def call(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        reply = json.loads(self.p.stdout.readline().decode("utf-8"))
        assert reply["id"] == self.next_id and reply["jsonrpc"] == "2.0"
        return reply

    def tool(self, name, arguments=None):
        reply = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        result = reply["result"]
        return result["isError"], result["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        assert self.p.wait(timeout=30) == 0
        self.p.stdout.close()
        self.p.stderr.close()


@pytest.fixture
def mcp(indexed_workspace):
    server = Mcp(indexed_workspace)
    yield server
    server.close()


def test_mcp_handshake_and_tool_list(mcp):
    init = mcp.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "context-foundation"
    assert "tools" in init["capabilities"]
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})      # 通知には返事が無い
    assert mcp.call("ping")["result"] == {}
    tools = mcp.call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "status"]                 # ツールはこの 2 つだけ
    search = tools[0]
    assert set(search["inputSchema"]["properties"]) == {"query", "top_k", "mode", "source_id"}
    assert search["inputSchema"]["required"] == ["query"] and search["annotations"]["readOnlyHint"] is True
    # ツールの定義は、使わない回でも毎回モデルに送られるので、小さく保つ
    assert len(json.dumps(tools)) < 1600


def test_mcp_search_and_status(mcp):
    mcp.call("initialize", {"protocolVersion": "2024-11-05"})
    is_error, text = mcp.tool("search", {"query": "job scheduler steal", "mode": "keyword", "top_k": 3})
    assert not is_error
    assert "[1] engine:Core/JobScheduler.cpp:" in text and "scope: kes::core::JobScheduler" in text
    assert "thread_local std::minstd_rand rng" in text                        # passage の本文まで返す
    assert len(text.encode("utf-8")) <= HARD_MAX_BYTES
    is_error, text = mcp.tool("search", {"query": "セーブデータを一時ファイルに書く", "source_id": "docs"})
    assert not is_error and "docs:save-data.md:" in text and "engine:" not in text
    is_error, text = mcp.tool("status")
    assert not is_error and "sources: engine (7 files" in text and "modes: hybrid, keyword, semantic" in text


def test_mcp_errors(mcp):
    mcp.call("initialize", {"protocolVersion": "1999-01-01"})
    assert mcp.tool("search", {})[0] is True
    assert mcp.tool("search", {"query": "x", "source_id": "nope"})[0] is True
    assert mcp.tool("search", {"query": "x", "mode": "bogus"})[0] is True
    assert mcp.call("tools/call", {"name": "write_file", "arguments": {}})["error"]["code"] == -32602
    assert mcp.call("resources/list")["error"]["code"] == -32601
    mcp.p.stdin.write(b"this is not json\n")
    mcp.p.stdin.flush()
    assert json.loads(mcp.p.stdout.readline())["error"]["code"] == -32700
    assert mcp.call("ping")["result"] == {}                                   # 誤りの後も動き続ける


def test_mcp_without_index_answers_instead_of_crashing(tmp_path):
    server = Mcp(tmp_path)
    try:
        server.call("initialize", {"protocolVersion": "2025-06-18"})
        is_error, text = server.tool("search", {"query": "anything"})
        assert is_error and "No context-foundation index" in text and "grep" in text
    finally:
        server.close()


def test_mcp_does_not_write_to_the_workspace(indexed_workspace):
    before = sorted((p.relative_to(indexed_workspace).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in indexed_workspace.rglob("*") if p.is_file())
    server = Mcp(indexed_workspace)
    server.call("initialize", {"protocolVersion": "2025-06-18"})
    server.tool("search", {"query": "buffer pool"})
    server.tool("status")
    server.close()
    after = sorted((p.relative_to(indexed_workspace).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                   for p in indexed_workspace.rglob("*") if p.is_file())
    assert before == after


# --- コマンドライン ---
def test_cli_index_search_status_eval_validate(tmp_path):
    write_config(tmp_path)
    r = run_cli("index", "--quiet", "--json", "--revision", "rev-42", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    built = json.loads(r.stdout)
    assert built["stats"]["files"] == 13 and built["embedding"]["model"] == "hash-256-test-only"
    assert (tmp_path / ".cf" / "index.db").is_file()

    r = run_cli("search", "job scheduler steal", "--mode", "keyword", "--format", "json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    top = json.loads(r.stdout)["passages"][0]
    assert top["path"] == "Core/JobScheduler.cpp" and top["rank"] == 1

    # 出力先が端末でないとき(エージェントから呼ばれたとき)は、エージェント向けの形
    sub = tmp_path / "deeper" / "folder"
    sub.mkdir(parents=True)
    r = run_cli("search", "セーブデータの検査用の値", cwd=sub)             # 親へさかのぼって .cf を探す
    assert r.returncode == 0 and r.stdout.startswith("context-foundation search:")
    assert "docs:save-data.md:" in r.stdout

    r = run_cli("status", "--format", "json", cwd=tmp_path)
    status = json.loads(r.stdout)
    assert status["stats"]["chunks"] == built["stats"]["chunks"] and status["revision"] == "rev-42"

    r = run_cli("validate", "--cases", str(CASES), "--check-corpus", cwd=tmp_path)
    assert r.returncode == 0 and "誤り 0 件" in r.stdout

    out = tmp_path / "eval-out"
    r = run_cli("eval", "--cases", str(CASES), "--out", str(out), "--run-id", "cli-run", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "# 評価の要約" in r.stdout and (out / "results.jsonl").is_file() and (out / "summary.json").is_file()
    run = json.loads((out / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert run["index"]["indexed_revision"] == "rev-42"          # 索引を作ったときの版が、評価の記録に残る
    r = run_cli("validate", "--cases", str(CASES), "--results", str(out / "results.jsonl"),
                "--runs", str(out / "runs.jsonl"), cwd=tmp_path)
    assert r.returncode == 0, r.stdout


def test_cli_without_config_tells_the_agent_to_fall_back(tmp_path):
    r = run_cli("search", "anything", cwd=tmp_path)
    assert r.returncode == 3 and "No context-foundation index" in r.stdout and "grep" in r.stdout


def test_cli_rejects_bad_config(tmp_path):
    (tmp_path / ".cf").mkdir()
    (tmp_path / ".cf" / "config.toml").write_text("[[source]]\nid = 'bad id'\nroot = '.'\n", encoding="utf-8")
    r = run_cli("index", cwd=tmp_path)
    assert r.returncode == 2 and "id が使えません" in r.stderr


def test_launcher_runs_the_cli_with_the_given_python(indexed_workspace):
    """skill が呼ぶ入口(ctxf.py)。標準ライブラリだけで動き、出力と終了コードをそのまま返す。"""
    env = dict(ENV, CF_PYTHON=sys.executable)
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "search", "buffer pool acquire",
                        "--mode", "keyword", "--top-k", "2"], cwd=indexed_workspace, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("context-foundation search: 2 passages") and "Render/BufferPool" in r.stdout


def test_session_hint_only_where_an_index_exists(indexed_workspace, tmp_path):
    """sessionStart の hook: 索引のある作業フォルダでだけ、skill の使いどころを 1 文で足す。"""
    def hint(cwd) -> str:
        r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint"], capture_output=True,
                           input=json.dumps({"cwd": str(cwd), "source": "new"}), text=True, encoding="utf-8",
                           cwd=tmp_path, timeout=60)
        assert r.returncode == 0
        return r.stdout

    sub = indexed_workspace / "some" / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    out = json.loads(hint(sub))
    assert "context-search skill" in out["additionalContext"] and len(out["additionalContext"]) < 400
    assert hint(tmp_path) == ""                       # 索引が無ければ、何も足さない
    # MCP サーバーも入れているとき(プラグイン): 検索の道具を先に呼ぶように促す。道具が無ければ skill、とも書く
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(sub)}), text=True, encoding="utf-8", cwd=tmp_path, timeout=60)
    ctx = json.loads(r.stdout)["additionalContext"]
    assert "context-foundation-search tool first" in ctx and "context-search skill" in ctx and len(ctx) < 700
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(tmp_path)}), text=True, encoding="utf-8", cwd=tmp_path, timeout=60)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint"], capture_output=True,
                       input="not json", text=True, cwd=tmp_path, timeout=60)
    assert r.returncode == 0 and r.stdout == ""       # 入力が壊れていても、セッションを止めない


def test_skill_file_points_at_the_launcher():
    folder = REPO / "skills" / "context-search"
    skill = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: context-search\ndescription: ")
    assert "ctxf.py\" search" in skill and (REPO / "ctxf.py").is_file()
    # 入口は、skill のフォルダの直下の取り次ぎから呼ぶ。「2 つ上」と書くと、エージェントがパスを組み立て
    # 間違える回があった(Godot での計測で、skill を読んだ 50 回のうち 15 回)
    assert '"<base directory of this skill>\\ctxf.py" search' in skill and "..\\" not in skill
    assert (folder / "ctxf.py").is_file()


def test_skill_folder_launcher_forwards_to_the_entry(tmp_path):
    def where(script):
        return subprocess.run([sys.executable, "-I", str(script), "where"], capture_output=True, encoding="utf-8",
                              errors="replace", cwd=tmp_path, timeout=60)
    via_skill, direct = where(REPO / "skills" / "context-search" / "ctxf.py"), where(REPO / "ctxf.py")
    assert via_skill.returncode == direct.returncode and direct.returncode in (0, 4)   # 4: 依存がまだ入っていない PC
    assert via_skill.stdout == direct.stdout and str(REPO / "ctxf.py") in via_skill.stdout


def test_mcp_warms_up_the_index_at_initialize(indexed_workspace, tmp_path):
    """起動の挨拶で、索引のベクトルと埋め込みを裏で先に読み込む。索引が無い・preload = false なら読み込まない。"""
    import shutil
    import threading

    from context_foundation import mcp_server

    def initialize(config: str):
        state = mcp_server._State(config)
        reply = mcp_server.handle(state, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert reply["result"]["serverInfo"]["name"] == "context-foundation" and state.warm_up_started
        assert mcp_server.start_warm_up(state) is None          # 2 回目は始めない
        for th in threading.enumerate():
            if th.name == "cf-warm-up":
                th.join(timeout=60)
        return state

    state = initialize(str(indexed_workspace))
    ((_, _, searcher),) = state.searchers.values()
    assert searcher._vectors is not None and searcher._embedder is not None
    is_error, text = mcp_server.call_tool(state, "search", {"query": "GPU buffer pool"})
    assert is_error is False and "passages" in text

    quiet = tmp_path / "quiet"                                  # preload = false: 最初の検索まで読み込まない
    shutil.copytree(indexed_workspace / ".cf", quiet / ".cf")
    with open(quiet / ".cf" / "config.toml", "a", encoding="utf-8") as f:
        f.write("\n[search]\npreload = false\n")
    state = initialize(str(quiet))
    ((_, _, searcher),) = state.searchers.values()
    assert searcher._vectors is None and searcher._embedder is None
    is_error, text = mcp_server.call_tool(state, "search", {"query": "GPU buffer pool"})
    assert is_error is False and searcher._vectors is not None

    empty = tmp_path / "nowhere"                                # 索引が無い: 何も読み込まず、落ちもしない
    empty.mkdir()
    state = initialize(str(empty / ".cf" / "config.toml"))
    assert state.searchers == {}


def test_warm_up_does_nothing_for_a_keyword_only_index(tmp_path):
    from context_foundation.config import load_config
    from context_foundation.indexer import build_index
    from context_foundation.search import Searcher

    from conftest import write_config

    cfg = load_config(write_config(tmp_path))
    build_index(cfg, use_embedding=False, progress=False, log=lambda s: None)
    searcher = Searcher.from_config(cfg)
    assert searcher.warm_up() is False and searcher._embedder is None


def test_mcp_finds_the_index_from_the_session_folder_the_host_reports(indexed_workspace, tmp_path, monkeypatch):
    """プラグインの MCP サーバーは、プラグインのフォルダを作業フォルダにして起動される。利用者の作業フォルダは、
    ホストが環境変数で知らせる。その場所から設定を探す。変数が無い・フォルダで無いときは、作業フォルダから探す。"""
    from context_foundation import mcp_server

    elsewhere = tmp_path / "plugin-folder"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("COPILOT_SESSION_CWD", raising=False)
    is_error, text = mcp_server.call_tool(mcp_server._State(None), "status", {})
    assert is_error and "no .cf folder found from " + str(elsewhere) in text

    monkeypatch.setenv("COPILOT_SESSION_CWD", str(indexed_workspace))
    is_error, text = mcp_server.call_tool(mcp_server._State(None), "search", {"query": "GPU buffer pool"})
    assert is_error is False and "passages" in text

    monkeypatch.setenv("COPILOT_SESSION_CWD", str(tmp_path / "gone"))
    is_error, text = mcp_server.call_tool(mcp_server._State(None), "status", {})
    assert is_error and str(elsewhere) in text
