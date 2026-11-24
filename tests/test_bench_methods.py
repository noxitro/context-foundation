"""bench の、Serena と言語サーバー(LSP)を足す手法(bench/methods.py と、2 つの流し手のコマンドの組み立て)。
エージェントも Serena も言語サーバーも起こさない。"""
from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from context_foundation.config import load_config

from conftest import CORPUS, write_config

REPO = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "bench" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def M():
    return load("methods")


@pytest.fixture(scope="module")
def agent_run():
    return load("agent_run")


@pytest.fixture(scope="module")
def claude_run():
    return load("claude_run")


def test_each_method_adds_only_its_own_tools(M):
    assert {m: sorted(M.features(m)) for m in M.METHODS} == {
        "agent_grep": [], "agent_grep_cxxref": ["cxxref"], "agent_cf": ["cf_skill"], "agent_cf_mcp": ["cf_mcp"],
        "agent_lsp": ["lsp"], "agent_cf_mcp_lsp": ["cf_mcp", "lsp"], "agent_serena": ["serena"],
        "agent_cf_mcp_serena": ["cf_mcp", "serena"]}
    assert set(M.CLAUDE_METHODS) <= set(M.METHODS) and "agent_cf" not in M.CLAUDE_METHODS
    # 手法の名前は、記録の形(results.schema.json の method)に合う
    import re
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", m) for m in M.METHODS)
    assert M.described_tools("agent_cf_mcp_serena", ["view"]) == ["view", "mcp:context-foundation", "mcp:serena"]
    assert M.described_tools("agent_lsp", ["Read"]) == ["Read", "lsp"]


def test_tool_names_of_both_hosts_fall_into_families(M):
    names = {"mcp__serena__find_symbol": "serena", "serena": "serena", "lsp": "lsp", "LSP": "lsp",
             "context-foundation": "search", "mcp__context-foundation__search": "search", "Grep": "grep",
             "grep": "grep", "view": "read", "Read": "read", "Glob": "glob", "bash": "shell", "powershell": "shell",
             "skill": "skill", "TodoWrite": "other"}
    assert {n: M.tool_family(n) for n in names} == names
    assert set(names.values()) <= set(M.FAMILIES)


def test_serena_results_give_symbol_ranges_in_one_based_lines(M):
    grouped = ('Found: {"engine/Render/BufferPool.cpp": [{"name_path": "BufferPool/Release", "kind": "Method", '
               '"body_location": {"start_line": 43, "end_line": 50}}], "engine/Render/BufferPool.h": '
               '{"Class": [{"name_path": "BufferPool", "body_location": {"start_line": 9, "end_line": 40}}]}}')
    assert M.serena_locations(grouped) == [("engine/Render/BufferPool.cpp", 44, 51), ("engine/Render/BufferPool.h", 10, 41)]
    flat = '[{"name_path": "X", "relative_path": "a/b.h", "body_location": {"start_line": 0, "end_line": 3}}]'
    assert M.serena_locations(flat) == [("a/b.h", 1, 4)]
    assert M.serena_locations("ValueError: No symbol matching 'X' found") == []
    assert M.serena_locations('{"name_path": "X", "body_location": {"start_line": 1, "end_line": 2}}') == []  # パスが無い


def test_serena_runs_read_only_without_its_dashboard(M, tmp_path):
    cmd = M.serena_server(tmp_path, "claude-code")
    assert cmd[:4] == ["uvx", "--from", f"serena-agent=={M.SERENA_VERSION}", "serena"]
    assert cmd[cmd.index("--project") + 1] == str(tmp_path) and cmd[cmd.index("--context") + 1] == "claude-code"
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--mode"] == ["planning", "benchmark"]
    assert cmd[cmd.index("--enable-web-dashboard") + 1] == "False" and cmd[cmd.index("--open-web-dashboard") + 1] == "False"
    assert not M.serena_project_ok(tmp_path) and "project create" in M.serena_setup_hint(tmp_path)
    (tmp_path / ".serena").mkdir()
    (tmp_path / ".serena" / "project.yml").write_text("read_only: true\n", encoding="utf-8")
    assert M.serena_project_ok(tmp_path)
    # 始めの指示は、1 回作ったら使い回す(流しの間は同じものを渡す)
    cache = tmp_path / "prompt.txt"
    cache.write_text("SERENA PROMPT", encoding="utf-8")
    assert M.serena_prompt(tmp_path, "claude-code", "never-run", {}, cache) == "SERENA PROMPT"


def test_the_lsp_plugin_for_claude_code_names_the_server_and_cpp_files(M, tmp_path):
    folder = M.lsp_plugin(tmp_path / "p", ["clangd", "--background-index"])
    assert json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"] == "bench-lsp"
    lsp = json.loads((folder / ".lsp.json").read_text(encoding="utf-8"))
    (server,) = lsp.values()
    assert (server["command"], server["args"]) == ("clangd", ["--background-index"])
    assert server["extensionToLanguage"][".cpp"] == "cpp" and server["extensionToLanguage"][".h"] == "cpp"


def copilot_args(cfg, tmp_path, method, lsp_config=None):
    return Namespace(method=method, model="anthropic/claude-haiku-5-5", workspace=cfg.workspace, cxxref=None,
                     max_ai_credits=0, trust_folder=False, provider="anthropic", lsp_config=lsp_config,
                     providers_config="~/.copilot/providers.json", no_custom_instructions=False, no_hint=False,
                     serena=None, hint_file=None)


@pytest.mark.parametrize("method", ["agent_lsp", "agent_cf_mcp_lsp", "agent_serena", "agent_cf_mcp_serena"])
def test_copilot_methods_add_lsp_and_serena_on_top_of_the_same_base(agent_run, M, tmp_path, method):
    cfg = load_config(write_config(tmp_path))
    roots = [s.root for s in cfg.sources]
    args = copilot_args(cfg, tmp_path, method, lsp_config=str(tmp_path / "lsp.json") if "lsp" in method else None)
    if "serena" in method:
        args.serena = {"project": tmp_path, "command": M.SERENA_COMMAND, "env": {"SERENA_HOME": str(tmp_path / "h")}}
        args.hint_file = tmp_path / "hint.txt"
    cmd, _ = agent_run.command_for(args, "copilot", "P", tmp_path / "u.json", tmp_path / "s.md", roots)
    tools = cmd[cmd.index("--available-tools") + 1].split(",")
    assert tools[:5] == agent_run.BASE_TOOLS                                     # 一式は既存の手順と同じ
    f = M.features(method)
    assert ("lsp" in tools) == ("lsp" in f) and ("serena" in tools) == ("serena" in f)
    assert (agent_run.MCP_NAME in tools) == ("cf_mcp" in f)
    allowed = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--allow-tool"]
    assert allowed[:2] == ["read", "shell(python:*)"] and cmd[cmd.index("--deny-tool") + 1] == "write"
    if "--additional-mcp-config" in cmd:
        servers = json.loads(cmd[cmd.index("--additional-mcp-config") + 1])["mcpServers"]
        assert set(servers) == ({agent_run.MCP_NAME} if "cf_mcp" in f else set()) | ({"serena"} if "serena" in f else set())
        if "serena" in f:
            assert servers["serena"]["env"] == {"SERENA_HOME": str(tmp_path / "h")}
            assert servers["serena"]["args"][servers["serena"]["args"].index("--context") + 1] == "copilot-cli"
    else:
        assert not f & {"cf_mcp", "serena"}
    hook = agent_run.hooks_for(args)
    if "serena" in f:                  # Serena の手法は、用意した文(Serena の指示と、この道具の 1 文)を渡す
        first = hook["hooks"]["sessionStart"][0]
        assert Path(first["args"][1]).name == "hint_hook.py" and first["args"][2] == str(tmp_path / "hint.txt")
    elif "cf_mcp" in f:
        assert hook["hooks"]["sessionStart"][0]["args"][2:] == ["session-hint", "--mcp"]
    else:
        assert hook is None


def test_the_old_lsp_config_on_other_methods_still_adds_the_lsp_tool(agent_run, tmp_path):
    cfg = load_config(write_config(tmp_path))
    args = copilot_args(cfg, tmp_path, "agent_cf_mcp", lsp_config=str(tmp_path / "lsp.json"))
    cmd, _ = agent_run.command_for(args, "copilot", "P", tmp_path / "u", tmp_path / "s", [s.root for s in cfg.sources])
    assert "lsp" in cmd[cmd.index("--available-tools") + 1].split(",")


def test_hint_hook_prints_the_prepared_text(tmp_path):
    import subprocess
    text = tmp_path / "hint.txt"
    text.write_text("Serena の指示\nと、この道具の 1 文", encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(REPO / "bench" / "hint_hook.py"), str(text)], input="{}",
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout) == {"additionalContext": "Serena の指示\nと、この道具の 1 文"}
    r = subprocess.run([sys.executable, "-I", str(REPO / "bench" / "hint_hook.py"), str(tmp_path / "none.txt")],
                       input="{}", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == ""                                 # hook はセッションを止めない


@pytest.mark.parametrize("method", ["agent_grep", "agent_cf_mcp", "agent_lsp", "agent_cf_mcp_lsp", "agent_serena",
                                    "agent_cf_mcp_serena"])
def test_claude_methods_restrict_tools_and_add_only_their_servers(claude_run, M, tmp_path, method):
    f = M.features(method)
    serena = {"project": tmp_path, "command": M.SERENA_COMMAND, "env": {"SERENA_HOME": "h"}} if "serena" in f else None
    servers = claude_run.mcp_servers(method, "py", tmp_path, serena)
    assert set(servers) == ({"context-foundation"} if "cf_mcp" in f else set()) | ({"serena"} if "serena" in f else set())
    plugin = M.lsp_plugin(tmp_path / "lsp", ["clangd"]) if "lsp" in f else None
    cmd = claude_run.build_command("claude", method, "haiku", [tmp_path], tmp_path / "mcp.json", "HINT" if f else "",
                                   plugin)
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert tools == ["Read", "Grep", "Glob"] + (["LSP"] if "lsp" in f else [])
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert ("mcp__serena" in allowed) == ("serena" in f)
    assert ("mcp__context-foundation__search" in allowed) == ("cf_mcp" in f)
    assert ("--plugin-dir" in cmd) == ("lsp" in f) and ("--append-system-prompt" in cmd) == bool(f)
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""
    assert not {"Bash", "Edit", "Write"} & set(tools)
    if "serena" in f:
        assert servers["serena"]["args"][servers["serena"]["args"].index("--context") + 1] == "claude-code"


def test_claude_lsp_method_needs_a_language_server(claude_run):
    with pytest.raises(SystemExit, match="--lsp-command"):
        claude_run.build_command("claude", "agent_lsp", "haiku", [], Path("m.json"), "", None)


def test_claude_stream_gives_opened_search_and_symbol_evidence(claude_run, agent_run, tmp_path):
    cfg = load_config(write_config(tmp_path))
    mapper = agent_run.Mapper(cfg)
    engine = CORPUS / "engine"
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "1", "name": "Read",
             "input": {"file_path": str(engine / "Render" / "BufferPool.cpp"), "offset": 10, "limit": 5}},
            {"type": "tool_use", "id": "2", "name": "mcp__context-foundation__search", "input": {"query": "q"}},
            {"type": "tool_use", "id": "3", "name": "mcp__serena__find_symbol", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "code"},
            {"type": "tool_result", "tool_use_id": "2",
             "content": [{"type": "text", "text": "context-foundation search: 1 passages\n\n"
                                                  "[1] engine:Audio/VoiceMixer.cpp:19-46  (function)\n"}]},
            {"type": "tool_result", "tool_use_id": "3",
             "content": '{"Core/Hash.cpp": [{"name_path": "Hash", "body_location": {"start_line": 4, "end_line": 9}}]}'}]}},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    got = claude_run.evidence_from_stream(raw, mapper, engine)
    assert [(e["origin"], e["source_id"], e["path"], e["start_line"], e["end_line"]) for e in got] == [
        ("opened", "engine", "Render/BufferPool.cpp", 10, 14),
        ("search_result", "engine", "Audio/VoiceMixer.cpp", 19, 46),
        ("symbol_result", "engine", "Core/Hash.cpp", 5, 10)]
    assert claude_run.returned_bytes(raw) > len("code")
    # 記録の形に合う(origin の symbol_result と、tools_by_name)
    from context_foundation.evalkit.validate import main as _  # noqa: F401  (読み込めること)
    schema = json.loads((REPO / "src" / "context_foundation" / "schemas" / "results.schema.json").read_text(encoding="utf-8"))
    import jsonschema
    row = {"run_id": "r", "case_id": "c", "method": "agent_cf_mcp_serena", "trial": 1, "status": "completed",
           "retrieved_evidence": [dict(rank=i + 1, **e) for i, e in enumerate(got)],
           "tools_by_name": {"Read": 1, "mcp__serena__find_symbol": 1}}
    jsonschema.validate(row, schema)


def test_the_claude_arms_example_names_every_claude_method_with_its_options(M):
    arms = json.loads((REPO / "bench" / "arms.claude.example.json").read_text(encoding="utf-8"))
    assert [a["method"] for a in arms.values()] == list(M.CLAUDE_METHODS)
    for a in arms.values():
        f = M.features(a["method"])
        assert a["runner"] == "claude_run" and a["args"][-1] == "--yes"
        assert ("--lsp-command" in a["args"]) == ("lsp" in f) and ("--serena-project" in a["args"]) == ("serena" in f)
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert all(n in manifest for n in ("bench/methods.py", "bench/hint_hook.py", "bench/arms.claude.example.json"))
