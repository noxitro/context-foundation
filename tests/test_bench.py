"""bench/agent_run.py の、Copilot CLI を呼ばない部分(パスの対応づけ、根拠の取り出し、記録の集計、コマンドの組み立て)。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from context_foundation.config import load_config

from conftest import CORPUS, write_config

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("agent_run", REPO / "bench" / "agent_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_run"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mapper(bench, tmp_path):
    return bench.Mapper(load_config(write_config(tmp_path)))


def test_paths_are_mapped_to_source_and_relative_path(bench, mapper):
    engine = CORPUS / "engine"
    assert mapper.resolve("engine:Audio/VoiceMixer.cpp")[:2] == ("engine", "Audio/VoiceMixer.cpp")
    assert mapper.resolve(str(engine / "Audio" / "VoiceMixer.cpp"))[:2] == ("engine", "Audio/VoiceMixer.cpp")
    assert mapper.resolve(str(engine).upper() + "\\Core\\Hash.cpp")[:2] == ("engine", "Core/Hash.cpp")
    assert mapper.resolve("`" + str(CORPUS / "docs" / "jobs.md") + "`")[:2] == ("docs", "jobs.md")
    assert mapper.resolve("C:/somewhere/else/file.cpp") is None


def test_evidence_is_read_from_the_answer(bench, mapper):
    engine = CORPUS / "engine"
    answer = ("VoiceMixer が混ぜています。\n\nEVIDENCE:\n"
              f"- engine:Audio/VoiceMixer.cpp:19-46\n- {engine}\\Audio\\VoiceMixer.cpp:9-17\n"
              "- `docs:jobs.md:12`\n- not a location\n\nこのあとの文は読まない\n- engine:Core/Hash.cpp:1-2\n")
    got = bench.parse_evidence(answer, mapper)
    assert [(e["source_id"], e["path"], e["start_line"], e["end_line"]) for e in got] == [
        ("engine", "Audio/VoiceMixer.cpp", 19, 46), ("engine", "Audio/VoiceMixer.cpp", 9, 17), ("docs", "jobs.md", 12, 12)]
    assert all(e["origin"] == "cited" for e in got)
    assert bench.parse_evidence("コーパスにありません。\n\nEVIDENCE: none\n", mapper) == []
    assert bench.parse_evidence("根拠の節が無い回答", mapper) == []


def test_cost_estimate_separates_cached_tokens(bench):
    prices = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}
    rec = {"input_tokens": 36705, "cache_read_tokens": 26939, "cache_write_tokens": 9756, "output_tokens": 1201}
    assert bench.cost_usd(rec, prices) == pytest.approx(0.00209, abs=1e-5)
    assert bench.cost_usd(rec, None) is None and bench.cost_usd({}, prices) is None


def test_session_records_are_collected(bench, mapper, tmp_path):
    """Copilot CLI が残す記録(events.jsonl と session-store.db)と同じ形のものから、見た場所と使用量を集める。"""
    home = tmp_path / "home"
    session = home / "session-state" / "abc-123"
    session.mkdir(parents=True)
    voice = CORPUS / "engine" / "Audio" / "VoiceMixer.cpp"
    search_out = ("context-foundation search: 2 passages, mode hybrid, index of x (33 passages)\n\n"
                  "[1] engine:Audio/VoiceMixer.cpp:19-46  (function)\nscope: kes::audio::VoiceMixer\n```\ncode\n```\n\n"
                  "More matches (text not shown):\n[2] docs:rendering.md:28-38  (heading)  Pass ordering\n")
    events = [
        {"type": "tool.execution_start", "data": {"toolName": "skill", "toolCallId": "1", "arguments": {}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "1", "result": {"content": "Skill loaded"}}},
        {"type": "tool.execution_start", "data": {"toolName": "powershell", "toolCallId": "2",
                                                  "arguments": {"command": "python -I ctxf.py search x"}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "2", "result": {"content": search_out}}},
        {"type": "tool.execution_start", "data": {"toolName": "view", "toolCallId": "3",
                                                  "arguments": {"path": str(voice), "view_range": [48, 61]}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "3", "result": {"content": "lines"}}},
        {"type": "tool.execution_start", "data": {"toolName": "view", "toolCallId": "4",
                                                  "arguments": {"path": str(CORPUS / "docs" / "jobs.md")}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "4", "result": {
            "content": "Output too large to read at once (30 KB). Saved to: x"}}},
        {"type": "assistant.message", "data": {"content": "答えです。\n\nEVIDENCE:\n- engine:Audio/VoiceMixer.cpp:19-46\n"}},
    ]
    (session / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    db = sqlite3.connect(home / "session-store.db")
    db.execute("create table assistant_usage_events(id integer primary key, session_id text, input_tokens int, "
               "cache_read_tokens int, cache_write_tokens int, output_tokens int, total_nano_aiu int)")
    db.executemany("insert into assistant_usage_events(session_id, input_tokens, cache_read_tokens, "
                   "cache_write_tokens, output_tokens, total_nano_aiu) values('abc-123', ?, ?, ?, ?, 0)",
                   [(100, 0, 90, 10), (150, 90, 50, 20)])
    db.commit()
    db.close()
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"totalNanoAiu": 250_000_000}), encoding="utf-8")

    rec = bench.collect(home, usage, mapper)
    assert rec["session_id"] == "abc-123" and rec["tool_calls"] == 4
    assert rec["tools"] == {"skill": 1, "powershell": 1, "view": 2}
    assert [(e["origin"], e["source_id"], e["path"], e["start_line"], e["end_line"]) for e in rec["retrieved"]] == [
        ("search_result", "engine", "Audio/VoiceMixer.cpp", 19, 46),
        ("search_result", "docs", "rendering.md", 28, 38),
        ("opened", "engine", "Audio/VoiceMixer.cpp", 48, 61),
        ("opened", "docs", "jobs.md", 1, 23)]                    # ファイル全体を開いたら、1 行目から最後の行まで
    assert rec["offloaded_results"] == 1 and rec["returned_bytes"] > len(search_out)
    assert (rec["model_calls"], rec["input_tokens"], rec["cache_read_tokens"], rec["output_tokens"]) == (2, 250, 90, 30)
    assert rec["ai_units"] == 0.25 and rec["answer"].startswith("答えです")


@pytest.mark.parametrize("method", ["agent_grep", "agent_cf", "agent_cf_mcp"])
def test_every_method_gets_the_same_tools_and_permissions(bench, tmp_path, method):
    cfg = load_config(write_config(tmp_path))
    roots = [s.root for s in cfg.sources]
    args = Namespace(method=method, model="anthropic/claude-haiku-5-5", workspace=cfg.workspace, cxxref=None,
                     max_ai_credits=0, trust_folder=False, provider="anthropic",
                     providers_config="~/.copilot/providers.json")
    cmd, env = bench.command_for(args, "copilot", "PROMPT", tmp_path / "u.json", tmp_path / "s.md", roots)
    tools = cmd[cmd.index("--available-tools") + 1].split(",")
    assert tools[:5] == bench.BASE_TOOLS and (tools[5:] == [bench.MCP_NAME]) == (method == "agent_cf_mcp")
    allowed = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--allow-tool"]
    assert allowed[:2] == ["read", "shell(python:*)"] and cmd[cmd.index("--deny-tool") + 1] == "write"
    assert ("COPILOT_SKILLS_DIRS" in env) == (method == "agent_cf")
    assert env["COPILOT_PROVIDERS_CONFIG"].endswith("providers.json")
    assert "--allow-all-tools" not in cmd and "--allow-all" not in cmd
    # コーパスが作業フォルダの外にあるので、読めるように渡す
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--add-dir"][:3] == [str(r) for r in roots]
