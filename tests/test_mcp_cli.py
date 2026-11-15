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
    r = run_cli("index", "--quiet", "--json", cwd=tmp_path)
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
    assert json.loads(r.stdout)["stats"]["chunks"] == built["stats"]["chunks"]

    r = run_cli("validate", "--cases", str(CASES), "--check-corpus", cwd=tmp_path)
    assert r.returncode == 0 and "誤り 0 件" in r.stdout

    out = tmp_path / "eval-out"
    r = run_cli("eval", "--cases", str(CASES), "--out", str(out), "--run-id", "cli-run", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "# 評価の要約" in r.stdout and (out / "results.jsonl").is_file() and (out / "summary.json").is_file()
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


def test_skill_file_points_at_the_launcher():
    skill = (REPO / "skills" / "context-search" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: context-search\ndescription: ")
    assert "ctxf.py\" search" in skill and (REPO / "ctxf.py").is_file()
