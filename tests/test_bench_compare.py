"""bench の比べる道具(compare.py・grade_pack.py・interleave.py・claude_run.py)の、外部を呼ばない部分。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"bench_{name}", REPO / "bench" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"bench_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def compare():
    return _load("compare")


def test_ids_of_takes_the_last_name_and_skips_plain_words(compare):
    point = "AZ::RHI::DeviceFence::WaitOnCpu blocks the CPU thread, and WaitOnCpuAsync uses a callback."
    assert compare.ids_of(point) == ["WaitOnCpu", "WaitOnCpuAsync"]
    assert compare.ids_of("The fence is signaled by the GPU.") == []
    # ありふれた名前(Init など)だけのときは、その手前の名前を使う
    assert compare.ids_of("BufferPool::Init creates it") == ["BufferPool"]


def test_id_recall_counts_whole_words_only(compare):
    case = {"category": "natural_language_only", "expected_answer_points": ["DeviceFence::WaitOnCpu waits.",
                                                                          "Fence::Init wraps it."]}
    assert compare.id_recall("It calls WaitOnCpu() on the device fence.", case) == pytest.approx(0.5)
    assert compare.id_recall("It calls WaitOnCpuAsync only.", case) == 0.0     # 別の名前の一部は数えない
    assert compare.id_recall("x", {"category": "unanswerable", "expected_answer_points": ["A::Bb"]}) is None


def test_paired_is_deterministic_and_skips_questions_without_both(compare):
    per = {f"q{i}": {"a": [0.6 + i * 0.01], "b": [0.5]} for i in range(10)}
    per["only_a"] = {"a": [1.0]}
    r1 = compare.paired(per, "a", "b")
    r2 = compare.paired(per, "a", "b")
    assert r1 == r2 and r1["questions"] == 10 and r1["better"] == 10 and r1["worse"] == 0
    assert r1["low"] <= r1["mean"] <= r1["high"] and r1["low"] > 0
    assert compare.paired({"q": {"a": [1.0], "b": [0.0]}}, "a", "b") is None      # 質問が 1 つでは区間を出さない


def test_load_drops_runs_that_did_not_finish(compare, tmp_path):
    res = tmp_path / "r.jsonl"
    cases = tmp_path / "c.jsonl"
    res.write_text("\n".join(json.dumps(r) for r in [
        {"case_id": "c1", "method": "a", "status": "completed", "answer": "x"},
        {"case_id": "c1", "method": "a", "status": "gave_up", "answer": ""}]), encoding="utf-8")
    cases.write_text(json.dumps({"case_id": "c1", "category": "x", "expected_answer_points": []}), encoding="utf-8")
    rows, cs = compare.load(res, cases)
    assert len(rows) == 1 and "c1" in cs


def test_grade_pack_hides_the_method_and_merges_back(tmp_path):
    gp = _load("grade_pack")
    res = tmp_path / "results.jsonl"
    cases = tmp_path / "cases.jsonl"
    rows = [{"run_id": "r", "case_id": "c1", "method": m, "trial": t, "answer": f"{m}{t}"}
            for m in ("grep", "cf") for t in (1, 2)]
    res.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    cases.write_text(json.dumps({"case_id": "c1", "question": "q", "category": "natural_language_only",
                                 "expected_answer_points": ["p"], "expected_evidence": []}), encoding="utf-8")
    out = tmp_path / "grade"
    assert gp.pack(res, cases, out, batches=2, seed=7) == 4
    items = [i for b in (1, 2) for i in json.loads((out / f"batch-{b}.json").read_text(encoding="utf-8"))]
    assert len(items) == 4 and all("method" not in i and "trial" not in i for i in items)
    (out / "score-1.json").write_text(json.dumps([{"item": i["item"], "score": 1.0 if i["answer"].startswith("cf")
                                                   else 0.5} for i in items]), encoding="utf-8")
    scored = tmp_path / "scored.jsonl"
    assert gp.merge(res, out, scored) == 4
    back = [json.loads(x) for x in scored.read_text(encoding="utf-8").splitlines()]
    assert {(r["method"], r["answer_correctness"]) for r in back} == {("cf", 1.0), ("grep", 0.5)}


def test_interleave_rotates_the_order(tmp_path):
    il = _load("interleave")
    assert [il.rotation(["a", "b", "c"], i) for i in range(3)] == [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]]
    arm = {"runner": "agent_run", "python": "py", "config": "ws", "method": "agent_grep", "out": "o", "run_id": "r",
           "args": ["--yes"]}
    cmd = il.command(arm, "cases.jsonl", "eval", 2, "C1")
    assert cmd[0] == "py" and cmd[1].endswith("agent_run.py") and cmd[-1] == "--yes"
    assert cmd[cmd.index("--case") + 1] == "C1" and cmd[cmd.index("--trials") + 1] == "2"


def test_claude_run_command_restricts_tools_and_settings(tmp_path):
    cr = _load("claude_run")
    cmd = cr.build_command("claude", "agent_cf_mcp", "haiku", [Path("D:/src")], tmp_path / "m.json",
                           tmp_path / "system-prompt.txt")
    assert cmd[cmd.index("--setting-sources") + 1] == ""          # 利用者の設定と指示のファイルを読ませない
    assert "--strict-mcp-config" in cmd and "--bare" not in cmd
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == str(tmp_path / "system-prompt.txt")
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__context-foundation__search" in allowed and "Bash" not in allowed
    # 既存の手順は、MCP サーバーの無い設定(手法ごとのファイル)を渡し、始めの文を足さない
    grep = cr.build_command("claude", "agent_grep", "haiku", [], tmp_path / "e.json", None)
    assert "--append-system-prompt-file" not in grep and grep[grep.index("--mcp-config") + 1].endswith("e.json")
    assert cr.mcp_servers("agent_grep", "py", tmp_path, None) == {}
    raw = "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        {"type": "result", "result": "answer", "usage": {"output_tokens": 3}}])
    final, calls, by_name = cr.parse_stream(raw + "\nnot json")
    assert final["result"] == "answer" and calls == 1 and by_name == {"Grep": 1}


def _rows_and_cases(tmp_path):
    cases = [{"case_id": "AR-01", "category": "natural_language_only", "question_language": "ja",
              "expected_answer_points": ["BufferPool::Release returns it."],
              "expected_evidence": [{"source_id": "engine", "path": "Render/BufferPool.cpp", "start_line": 40,
                                     "end_line": 50}]},
             {"case_id": "BR-01", "category": "cross_language", "question_language": "en",
              "expected_answer_points": ["VoiceMixer::Mix mixes."], "expected_evidence": []}]
    ev = {"source_id": "engine", "path": "Render/BufferPool.cpp", "start_line": 44, "end_line": 46}
    rows = [
        {"run_id": "r", "case_id": "AR-01", "method": "agent_grep", "trial": 1, "status": "completed",
         "answer": "Release", "answer_correctness": 1.0, "task_time_seconds": 10, "cost_usd": 0.02, "input_tokens": 100,
         "output_tokens": 10, "tool_calls": 3, "returned_bytes": 2048,
         "retrieved_evidence": [dict(rank=1, origin="opened", **ev)], "tools_by_name": {"grep": 2, "view": 1}},
        {"run_id": "r", "case_id": "AR-01", "method": "agent_serena", "trial": 1, "status": "completed",
         "answer": "Release", "answer_correctness": 0.5, "task_time_seconds": 20, "cost_usd": 0.04,
         "retrieved_evidence": [dict(rank=1, origin="cited", **ev)],
         "tools_by_name": {"mcp__serena__find_symbol": 2, "Read": 1}},
        {"run_id": "r", "case_id": "BR-01", "method": "agent_serena", "trial": 1, "status": "timeout",
         "answer": None, "retrieved_evidence": []},
        {"run_id": "r", "case_id": "BR-01", "method": "agent_grep", "trial": 1, "status": "completed",
         "answer": "Mix", "answer_correctness": "acceptable", "task_time_seconds": 30, "retrieved_evidence": []},
    ]
    r, c = tmp_path / "r.jsonl", tmp_path / "c.jsonl"
    r.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    c.write_text("\n".join(json.dumps(x) for x in cases), encoding="utf-8")
    return r, c


def test_summary_counts_failures_evidence_and_groups(compare, tmp_path):
    r, c = _rows_and_cases(tmp_path)
    rows_all, cases = compare.load_all(r, c)
    (_, table), *groups = compare.summary_tables(rows_all, cases, "area")
    by = {t["method"]: t for t in table}
    assert (by["agent_serena"]["runs"], by["agent_serena"]["timeout"], by["agent_serena"]["completed"]) == (2, 1, 1)
    # 根拠の当たり: 回答が根拠に挙げた場所(cited)だけ / 開いた場所も含める(seen)。正解の場所の無い質問は除く
    assert (by["agent_grep"]["evidence_cited"], by["agent_grep"]["evidence_seen"]) == (0.0, 1.0)
    assert (by["agent_serena"]["evidence_cited"], by["agent_serena"]["evidence_seen"]) == (1.0, 1.0)
    # 文字の採点(acceptable)は、点の平均に入れない
    assert (by["agent_grep"]["scored"], by["agent_grep"]["pass"]) == (1, 1)
    assert [g for g, _ in groups] == ["area: AR", "area: BR"]
    assert compare.group_of(cases["AR-01"], "language") == "ja"
    assert compare.group_of(cases["BR-01"], "category") == "cross_language"


def test_cost_and_tool_tables(compare, tmp_path):
    r, c = _rows_and_cases(tmp_path)
    rows = compare.load(r, c)[0]
    grep = [x for x in rows if x["method"] == "agent_grep"]
    cost = compare.cost_row(grep)
    assert (cost["time_median"], cost["cost_total"], cost["returned_kb_median"]) == (20, 0.02, 2.0)
    tools = compare.tools_row([x for x in rows if x["method"] == "agent_serena"])
    assert (tools["serena_per_run"], tools["serena_used"], tools["read_per_run"], tools["grep_used"]) == (2, 1.0, 1, 0.0)
    assert compare.tools_row(grep)["runs"] == 1                                # tools_by_name の無い回は除く
    text = compare.render([dict(method="agent_grep", **cost)], compare.COST_COLUMNS, "markdown")
    assert text.splitlines()[0].startswith("| method | runs | time_s") and "| agent_grep | 2 |" in text


def test_cli_prints_every_table(compare, tmp_path):
    import subprocess
    r, c = _rows_and_cases(tmp_path)
    for args in (["summary", "--by", "language"], ["cost"], ["tools"], ["summary", "--format", "json"],
                 ["paired", "agent_serena", "agent_grep"]):
        cmd = [sys.executable, str(REPO / "bench" / "compare.py"), args[0], str(r), str(c), *args[1:]]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert p.returncode == 0, p.stderr
        assert p.stdout.strip()
    assert "language: ja" in subprocess.run([sys.executable, str(REPO / "bench" / "compare.py"), "summary", str(r),
                                             str(c), "--by", "language"], capture_output=True, text=True,
                                            encoding="utf-8").stdout
