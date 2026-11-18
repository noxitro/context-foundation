"""MCP サーバー(stdio の JSON-RPC)と、コマンドラインを、別プロセスで起動して通しで確かめる。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from context_foundation.render import HARD_MAX_BYTES

from conftest import CASES, CORPUS, write_config

REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, CF_TEST_EMBEDDER="hash", PYTHONIOENCODING="utf-8")
ENV.pop("CF_CONFIG", None)
ENV.pop("COPILOT_SESSION_CWD", None)       # MCP サーバーは、作業フォルダをこの変数から探す(Copilot のシェルでは入っている)
# 子のプロセスにも、このフォルダの src を先に読ませる(pyproject の pythonpath はテストのプロセスの中にしか効かない。
# 無いと、子は editable で入れたときのフォルダのコードを試す。フォルダを動かしたり写したりしたときに混ざる)
ENV["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH")]))


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


def test_child_processes_run_the_code_of_this_folder(tmp_path):
    """run_cli と Mcp が起動する子は、このフォルダの src のパッケージを読む(入れたときの別のフォルダのものではなく)。"""
    r = subprocess.run([sys.executable, "-c", "import context_foundation; print(context_foundation.__file__)"],
                       cwd=tmp_path, env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert Path(r.stdout.strip()).resolve().is_relative_to((REPO / "src").resolve()), r.stdout + r.stderr


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
    assert not is_error and "modes: hybrid, keyword, semantic" in text
    # source ごとのフォルダも出す(このテストのコーパスは作業フォルダの外なので、絶対のパス)
    assert f"engine = {CORPUS / 'engine'} (7 files" in text and "Open source:path as <root>/<path>" in text


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


def test_mcp_without_index_answers_instead_of_crashing(tmp_path, outside_any_workspace):
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


def test_workspace_config_cannot_load_code(indexed_workspace, tmp_path):
    """取ってきたリポジトリの .cf/config.toml が reranker を名指ししても、サーバーはコードを読み込まない。"""
    import shutil
    ws = tmp_path / "ws"
    (ws / ".cf").mkdir(parents=True)
    shutil.copy(indexed_workspace / ".cf" / "index.db", ws / ".cf" / "index.db")
    marker = tmp_path / "marker.txt"
    write_config(ws, extra="[search]\nreranker = 'cProfile:run'\n")
    server = Mcp(ws)
    try:
        server.call("initialize", {"protocolVersion": "2025-06-18"})
        is_error, text = server.tool("search", {"query": f"open({str(marker)!r}, 'w').write('ran')",
                                                "mode": "keyword"})
        assert is_error and "reranker" in text and "0.4.0" in text
        assert server.call("ping")["result"] == {}
    finally:
        server.p.stdin.close()
        rest = server.p.stdout.read().decode("utf-8")
        server.p.wait(timeout=30)
        server.p.stdout.close()
        server.p.stderr.close()
    assert not marker.exists()
    assert all(json.loads(line) for line in rest.splitlines() if line.strip())     # 標準出力は JSON-RPC だけ


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


def test_cli_without_config_tells_the_agent_to_fall_back(tmp_path, outside_any_workspace):
    r = run_cli("search", "anything", cwd=tmp_path)
    assert r.returncode == 3 and "No context-foundation index" in r.stdout and "grep" in r.stdout


def test_cli_rejects_bad_config(tmp_path):
    (tmp_path / ".cf").mkdir()
    (tmp_path / ".cf" / "config.toml").write_text("[[source]]\nid = 'bad id'\nroot = '.'\n", encoding="utf-8")
    r = run_cli("index", cwd=tmp_path)
    assert r.returncode == 2 and "id が使えません" in r.stderr


def test_cli_agent_output_counts_the_limit_with_lf_newlines(indexed_workspace):
    """上限は、改行を LF として数えたバイト数。Windows で CLI の標準出力を受けると改行が CRLF になり、行の数だけ
    増える(0.3.1 から同じ。MCP は JSON の文字列なので増えない)。"""
    r = subprocess.run([sys.executable, "-m", "context_foundation", "search", "buffer pool render queue", "--top-k", "20",
                        "--format", "agent", "--max-bytes", "1000"], cwd=indexed_workspace, env=ENV,
                       capture_output=True, timeout=180)
    assert r.returncode == 0, r.stderr
    lf = r.stdout.replace(b"\r\n", b"\n")
    assert len(lf) <= 1000 and b"More matches (text not shown):" in lf
    if sys.platform == "win32":
        assert len(r.stdout) - len(lf) == lf.count(b"\n")
    else:
        assert r.stdout == lf


def test_launcher_runs_the_cli_with_the_given_python(indexed_workspace):
    """skill が呼ぶ入口(ctxf.py)。標準ライブラリだけで動き、出力と終了コードをそのまま返す。"""
    env = dict(ENV, CF_PYTHON=sys.executable)
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "search", "buffer pool acquire",
                        "--mode", "keyword", "--top-k", "2"], cwd=indexed_workspace, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("context-foundation search: 2 passages") and "Render/BufferPool" in r.stdout


def test_session_hint_only_where_an_index_exists(indexed_workspace, tmp_path, outside_any_workspace):
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
    # MCP サーバーも入れているとき(プラグイン): 検索の道具を先に呼ぶように促す。道具が無ければ skill、とも書く。
    # 用意が済んでいる PC での文面を見るので、使う python を CF_PYTHON で渡す(隣に .venv が無い環境でも同じ結果)
    ready = dict(os.environ, CF_PYTHON=sys.executable)
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(sub)}), text=True, encoding="utf-8", cwd=tmp_path, env=ready,
                       timeout=60)
    ctx = json.loads(r.stdout)["additionalContext"]
    assert "context-foundation-search tool first" in ctx and "context-search skill" in ctx and len(ctx) < 700
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(tmp_path)}), text=True, encoding="utf-8", cwd=tmp_path, env=ready,
                       timeout=60)
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


def test_mcp_finds_the_index_from_the_session_folder_the_host_reports(indexed_workspace, tmp_path, monkeypatch,
                                                                       outside_any_workspace):
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


# --- 壊れた要求(MCP-4)、長すぎる質問(MCP-8)、索引の無い作業フォルダの挨拶(MCP-9)、鮮度(DOC-1)

def test_server_survives_malformed_requests(mcp):
    mcp.call("initialize", {"protocolVersion": "2025-06-18"})
    raw = [
        '{"jsonrpc":"2.0","id":10,"method":"initialize","params":[1,2]}',
        '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":"x"}',
        '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"\\ud83d","arguments":{}}}',
        '{"jsonrpc":"2.0","id":13,"method":"x\\ud83d"}',
        '{"jsonrpc":"2.0","id":14,"method":"tools/call",'
        '"params":{"name":"search","arguments":{"query":"x","source_id":"a\\ud83d"}}}',
        '{"jsonrpc":"2.0","id":"\\ud83d","method":"ping"}',
        "[" * 200_000,
        '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',                   # NaN・Infinity は JSON ではない
        '{"jsonrpc":"2.0","id":-Infinity,"method":"nope"}',
        '{"jsonrpc":"2.0","id":1e999,"method":"ping"}',                 # JSON としては正しいが、inf になる
    ]
    # 決まった答えのあるもの: 要求の id → 誤りの符号(None は、誤りでなく result が返る)
    expected = [-32602, -32602, -32602, -32601, None, None, -32700, -32700, -32700, -32600]

    def not_json(name):
        raise AssertionError(f"JSON でない定数 {name} が返事に入っている")

    for line, code in zip(raw, expected, strict=True):
        mcp.p.stdin.write(line.encode("utf-8") + b"\n")
        mcp.p.stdin.flush()
        reply = json.loads(mcp.p.stdout.readline(), parse_constant=not_json)     # どれにも JSON の 1 行で答える
        assert reply["jsonrpc"] == "2.0"
        if code is None:
            assert "result" in reply, (line[:80], reply)
        else:
            assert reply["error"]["code"] == code, (line[:80], reply)
        if code in (-32700, -32600):
            assert reply["id"] is None
    assert mcp.call("ping")["result"] == {}                          # 落ちずに動き続ける


def _serve_lines(monkeypatch, tmp_path, lines: list[str]) -> list[bytes]:
    """serve() を同じプロセスで動かし、要求の行を順に渡して、標準出力に書かれた行を返す。"""
    import io

    from context_foundation import mcp_server
    monkeypatch.setenv("COPILOT_SESSION_CWD", str(tmp_path))
    out = io.BytesIO()
    assert mcp_server.serve(stdin=io.BytesIO("".join(x + "\n" for x in lines).encode("utf-8")), stdout=out) == 0
    return out.getvalue().splitlines(keepends=True)


def test_an_unexpected_exception_is_answered_and_the_server_lives(monkeypatch, tmp_path):
    from context_foundation import mcp_server

    def broken(state, name, args):
        raise RuntimeError("boom")
    monkeypatch.setattr(mcp_server, "call_tool", broken)
    out = _serve_lines(monkeypatch, tmp_path, [
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"status","arguments":{}}}',
        '{"jsonrpc":"2.0","id":2,"method":"ping"}'])
    assert [json.loads(x) for x in out] == [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}},
        {"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_a_reply_that_cannot_be_encoded_becomes_one_fixed_json_line(monkeypatch, tmp_path):
    from context_foundation import mcp_server
    real = mcp_server.handle

    def nan_reply(state, msg):
        if msg.get("id") == 1:
            return {"jsonrpc": "2.0", "id": 1, "result": {"x": float("nan")}}
        return real(state, msg)
    monkeypatch.setattr(mcp_server, "handle", nan_reply)
    out = _serve_lines(monkeypatch, tmp_path, ['{"jsonrpc":"2.0","id":1,"method":"ping"}',
                                               '{"jsonrpc":"2.0","id":2,"method":"ping"}'])
    assert out[0] == mcp_server.ENCODE_FAILED
    assert json.loads(out[0])["error"]["code"] == -32603
    assert json.loads(out[1]) == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_search_rejects_oversized_query(mcp):
    mcp.call("initialize", {"protocolVersion": "2025-06-18"})
    is_error, text = mcp.tool("search", {"query": "buffer pool " * 1000})
    assert is_error and "query too long (12,000 bytes" in text
    assert mcp.tool("search", {"query": "buffer pool " * 600})[0] is False      # 7,200 バイトは通る


def test_initialize_instructions_depend_on_index(indexed_workspace, tmp_path, outside_any_workspace):
    from context_foundation import mcp_server
    server = Mcp(indexed_workspace)
    try:
        init = server.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        assert init["instructions"] == mcp_server.INSTRUCTIONS               # 索引のある作業フォルダは 0.3 のまま
        assert init["instructions"].startswith("Call `search` first, before grepping")
        tools = server.call("tools/list")["result"]["tools"]
        assert tools == mcp_server.TOOLS
    finally:
        server.close()
    server = Mcp(tmp_path)
    try:
        init = server.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        assert init["instructions"] == "No context-foundation index in this workspace; search with grep."
        assert [t["name"] for t in server.call("tools/list")["result"]["tools"]] == ["search", "status"]
    finally:
        server.close()


# --- 測った構成の出力が 0.3.1 と同じこと(不変条件 1)。正解は tests/fixtures/golden/ に、0.3.1 のコードで作ったもの
# (フィクスチャのコーパスを写し、source のルートを '.' にした作業フォルダ。埋め込みはテスト用のハッシュ)。
# 定数どうしでは比べない: 道具の説明・指示・hook の文・出力の形を変えると、ここで落ちる

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
# 正解のファイル → search の引数(先頭の行の索引の日時だけ <CREATED_AT> にしてある)
GOLDEN_SEARCHES = {
    "search_keyword_top3.txt": ["job scheduler steal", "--mode", "keyword", "--top-k", "3"],
    "search_hybrid_default.txt": ["save data checksum written before power loss"],
    "search_keyword_top20_3000.txt": ["buffer", "--mode", "keyword", "--top-k", "20", "--max-bytes", "3000"],
}


def _golden(name: str) -> bytes:
    return (GOLDEN / name).read_bytes()


def _normalize_date(text: str) -> bytes:
    """先頭の行の索引の日時を、正解と同じ <CREATED_AT> にする(ほかは 1 バイトも変えない)。"""
    import re
    out, n = re.subn(r"\A(context-foundation search: [^\n]*?, index of )\S+( \()", r"\1<CREATED_AT>\2", text)
    assert n == 1, text[:200]
    return out.encode("utf-8")


@pytest.fixture(scope="module")
def root_workspace(tmp_path_factory) -> Path:
    """フィクスチャのコーパスを写し、source のルートを作業フォルダそのもの('.')にした作業フォルダ(測った形)。"""
    import shutil
    ws = tmp_path_factory.mktemp("rootws") / "ws"
    shutil.copytree(CORPUS, ws)
    (ws / ".cf").mkdir()
    (ws / ".cf" / "config.toml").write_text("[[source]]\nid = 'ws'\nroot = '.'\n", encoding="utf-8")
    r = run_cli("index", "--quiet", cwd=ws)
    assert r.returncode == 0, r.stderr
    return ws


@pytest.mark.parametrize("name", list(GOLDEN_SEARCHES))
def test_cli_agent_output_is_the_031_output(root_workspace, name):
    r = run_cli("search", *GOLDEN_SEARCHES[name], "--format", "agent", cwd=root_workspace)
    assert r.returncode == 0, r.stderr
    assert _normalize_date(r.stdout) == _golden(name)


def test_mcp_text_is_the_031_text(root_workspace):
    server = Mcp(root_workspace)
    try:
        init = server.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        tools = server.call("tools/list")["result"]["tools"]
        is_error, text = server.tool("search", {"query": "job scheduler steal", "mode": "keyword", "top_k": 3})
    finally:
        server.close()
    assert init["instructions"].encode("utf-8") == _golden("mcp_instructions.txt")
    # 説明・引数の定義と、項目の並びまで同じ(道具の定義は、毎回モデルに送られる)
    assert (json.dumps(tools, ensure_ascii=False, indent=1) + "\n").encode("utf-8") == _golden("mcp_tools.json")
    assert not is_error and _normalize_date(text) == _golden("search_keyword_top3.txt")


def test_session_hint_for_a_ready_indexed_workspace_is_the_031_text(root_workspace, tmp_path):
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(root_workspace)}).encode("utf-8"), cwd=tmp_path,
                       env=dict(os.environ, CF_PYTHON=sys.executable), timeout=60)
    assert r.returncode == 0 and r.stdout == _golden("session_hint_mcp.json")


def test_golden_files_hold_nothing_of_this_machine():
    """正解のファイルに、作った PC のパスや名前が入っていない(パスの区切りやドライブの形も無い)。LF・BOM なし。"""
    for f in GOLDEN.iterdir():
        data = f.read_bytes()
        assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf"), f.name
        text = data.decode("utf-8")
        assert "\\" not in text and ":/" not in text and "/home/" not in text and "Users" not in text, f.name


def _workspace_with_config(indexed_workspace: Path, dest: Path, config: bytes | None = None,
                           with_index: bool = True) -> Path:
    """索引のある作業フォルダの写し(設定のバイト列を差し替えられる)。"""
    import shutil
    (dest / ".cf").mkdir(parents=True)
    src = indexed_workspace / ".cf"
    shutil.copy2(src / "config.toml", dest / ".cf" / "config.toml")
    if with_index:
        shutil.copy2(src / "index.db", dest / ".cf" / "index.db")
    if config is not None:
        (dest / ".cf" / "config.toml").write_bytes(config)
    return dest


@pytest.mark.parametrize("kind", ["cp932", "utf-16", "search-not-a-table", "index-array-of-tables"])
def test_initialize_survives_unreadable_config(indexed_workspace, tmp_path, kind):
    """設定が UTF-8 でない・表の型が違うときも、initialize は答え(索引があるとみなす)、理由は道具の返事と CLI の
    1 行で伝える(0.3.1 と同じ。traceback は出さない)。"""
    from context_foundation import mcp_server
    text = (indexed_workspace / ".cf" / "config.toml").read_text(encoding="utf-8")
    config = {"cp932": ("# 設定\n" + text).encode("cp932"),
              "utf-16": ("# 設定\n" + text).encode("utf-16"),
              "search-not-a-table": ("search = 5\n" + text).encode("utf-8"),
              "index-array-of-tables": (text + "\n[[index]]\n").encode("utf-8")}[kind]
    ws = _workspace_with_config(indexed_workspace, tmp_path / "ws", config)
    why = "UTF-8" if kind in ("cp932", "utf-16") else "表"
    server = Mcp(ws)
    try:
        init = server.call("initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["instructions"] == mcp_server.INSTRUCTIONS
        is_error, msg = server.tool("search", {"query": "job scheduler"})
        assert is_error and why in msg
        assert server.tool("status")[0] is True
    finally:
        server.close()
    r = run_cli("status", cwd=ws)
    assert r.returncode == 2 and "Traceback" not in r.stderr and why in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1


def test_a_config_that_cannot_be_opened_is_one_line_and_exit_code_2(monkeypatch, indexed_workspace, tmp_path,
                                                                     capsys):
    """設定ファイルを開けない(権限が無いなど)とき: 「設定ファイルを開けません」の 1 行で、終了コード 2。"""
    from context_foundation import cli
    from context_foundation import config as config_module
    from context_foundation.config import ConfigError, load_config
    ws = _workspace_with_config(indexed_workspace, tmp_path / "ws")

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(config_module, "open", denied, raising=False)
    with pytest.raises(ConfigError, match="設定ファイルを開けません"):
        load_config(ws / ".cf" / "config.toml")
    assert cli.main(["status", "--config", str(ws / ".cf" / "config.toml")]) == 2
    err = capsys.readouterr().err
    assert "設定ファイルを開けません" in err and "Permission denied" in err and len(err.strip().splitlines()) == 1


def test_has_index_counts_any_failure_to_read_the_config_as_an_index(monkeypatch, indexed_workspace):
    from context_foundation import mcp_server

    def fails(path):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(mcp_server, "load_config", fails)
    monkeypatch.setenv("COPILOT_SESSION_CWD", str(indexed_workspace))
    assert mcp_server.has_index(mcp_server._State(None)) is True


def test_initialize_with_a_broken_config_or_a_missing_index_file(indexed_workspace, tmp_path):
    """TOML の誤り → 索引があるとみなして元の指示。設定はあるが index.db が無い → 索引の無い 1 文。"""
    from context_foundation import mcp_server
    cases = [(_workspace_with_config(indexed_workspace, tmp_path / "broken", b"this is [not toml"),
              mcp_server.INSTRUCTIONS),
             (_workspace_with_config(indexed_workspace, tmp_path / "noindex", with_index=False),
              mcp_server.INSTRUCTIONS_NO_INDEX)]
    for ws, want in cases:
        server = Mcp(ws)
        try:
            assert server.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"] == want
        finally:
            server.close()


def test_zero_hit_message_names_the_index_date(mcp, indexed_workspace):
    mcp.call("initialize", {"protocolVersion": "2025-06-18"})
    is_error, text = mcp.tool("search", {"query": "zzzqqq nothingmatches", "mode": "keyword"})
    assert not is_error and "No passage matched" in text
    created = json.loads(run_cli("status", "--format", "json", cwd=indexed_workspace).stdout)["created_at"]
    assert f"index of {created}" in text


def test_status_check_reports_freshness(indexed_workspace):
    r = run_cli("status", "--check", "--format", "json", cwd=indexed_workspace)
    assert r.returncode == 0, r.stderr
    fr = json.loads(r.stdout)["freshness"]
    assert (fr["changed"], fr["added"], fr["deleted"], fr["missing_roots"]) == (0, 0, 0, [])
    r = run_cli("status", "--check", "--format", "agent", cwd=indexed_workspace)
    assert "since the index: 0 files changed, 0 added, 0 deleted" in r.stdout
    assert "since the index" not in run_cli("status", "--format", "agent", cwd=indexed_workspace).stdout
