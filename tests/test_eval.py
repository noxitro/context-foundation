"""評価: 一致の判定と指標、しきい値の合否、データの検査、実行と取り込み。"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from context_foundation.evalkit.data import check_cases, check_results, load_cases, read_jsonl
from context_foundation.evalkit.metrics import acceptable, aggregate, percentile, score_result
from context_foundation.evalkit.runner import main as eval_main
from context_foundation.evalkit.summary import DEFAULT_THRESHOLDS, build_summary, judge
from context_foundation.evalkit.validate import main as validate_main

from conftest import CASES


def ev(path, a, b, source="engine", rank=None):
    d = {"source_id": source, "path": path, "start_line": a, "end_line": b}
    if rank is None:
        d["rationale"] = "r"
    else:
        d["rank"] = rank
    return d


def case(cid="Q1", evidence=None, category="natural_language_only", lang="ja"):
    evidence = [ev("a/File.cpp", 10, 20)] if evidence is None else evidence
    return {"case_id": cid, "question": "q", "question_language": lang, "category": category,
            "expected_evidence": evidence, "expected_answer_points": ["p"]}


def result(cid, method, retrieved, trial=1, run="r1", **extra):
    base = {"run_id": run, "case_id": cid, "method": method, "trial": trial, "status": "completed",
            "retrieved_evidence": retrieved, "search_time_ms": None if method.startswith("agent_") else 5.0,
            "answer": None, "answer_correctness": None, "task_time_seconds": None, "tool_calls": None}
    base.update(extra)
    return base


# --- 一致の判定と指標 ---
def test_passage_and_file_match():
    c = case()
    s = score_result(c, result("Q1", "keyword", [
        ev("a/Other.cpp", 1, 5, rank=1),       # 別のファイル
        ev("a/File.cpp", 30, 40, rank=2),      # 同じファイルだが、行が重ならない → ファイルの一致だけ
        ev("a/File.cpp", 20, 25, rank=3),      # 1 行重なる → passage の一致
    ]))
    assert s["file_rank"] == 2 and s["passage_rank"] == 3
    assert (s["hit1_file"], s["hit5_file"], s["hit1_passage"], s["hit5_passage"]) == (0.0, 1.0, 0.0, 1.0)
    assert s["rr_passage"] == pytest.approx(1 / 3) and s["rr_file"] == 0.5
    assert s["evidence_recall"] == 1.0


def test_match_normalizes_path_and_respects_source():
    c = case(evidence=[ev("Dir/File.cpp", 10, 20)])
    assert score_result(c, result("Q1", "keyword", [ev("dir\\file.CPP", 20, 20, rank=1)]))["passage_rank"] == 1
    other = score_result(c, result("Q1", "keyword", [ev("Dir/File.cpp", 10, 20, source="app", rank=1)]))
    assert other["passage_rank"] is None and other["file_rank"] is None


def test_evidence_recall_counts_expected_items_found_in_top_10():
    c = case(evidence=[ev("a.cpp", 1, 5), ev("b.cpp", 1, 5), ev("c.cpp", 1, 5), ev("d.cpp", 1, 5)])
    retrieved = [ev("a.cpp", 5, 9, rank=1), ev("b.cpp", 1, 1, rank=10), ev("c.cpp", 1, 5, rank=11)]
    assert score_result(c, result("Q1", "keyword", retrieved))["evidence_recall"] == 0.5


def test_unanswerable_cases_are_left_out_of_retrieval_metrics():
    cases = {"Q1": case(), "U1": case("U1", evidence=[], category="unanswerable")}
    rows = [result("Q1", "keyword", [ev("a/File.cpp", 10, 12, rank=1)]), result("U1", "keyword", [])]
    (m,) = aggregate(cases, rows)
    assert m["retrieval"]["cases"] == 1 and m["retrieval"]["hit1_passage"] == 1.0
    assert m["unanswerable_cases"] == 1


def test_trials_are_averaged_per_case_then_over_cases():
    cases = {"Q1": case(), "Q2": case("Q2")}
    hit, miss = [ev("a/File.cpp", 10, 12, rank=1)], [ev("x.cpp", 1, 2, rank=1)]
    rows = [result("Q1", "agent_grep", hit, 1, answer="a", task_time_seconds=10, answer_correctness=1.0),
            result("Q1", "agent_grep", miss, 2, answer="a", task_time_seconds=30, answer_correctness=0.5),
            result("Q1", "agent_grep", miss, 3, answer="a", task_time_seconds=20, answer_correctness="acceptable"),
            result("Q2", "agent_grep", hit, 1, answer="a", task_time_seconds=40, answer_correctness=0.9)]
    (m,) = aggregate(cases, rows)
    assert m["retrieval"]["hit1_passage"] == pytest.approx((1 / 3 + 1.0) / 2)
    assert m["answers"]["acceptable_rate"] == pytest.approx((2 / 3 + 1.0) / 2)
    assert m["task_time_seconds_median"] == 25 and m["trials"] == 3 and m["agent"] is True


def test_breakdown_by_category_and_language():
    cases = {"Q1": case(), "Q2": case("Q2", category="doc_to_code", lang="en")}
    rows = [result("Q1", "keyword", [ev("a/File.cpp", 10, 12, rank=1)]),
            result("Q2", "keyword", [ev("zzz.cpp", 1, 2, rank=1)])]
    (m,) = aggregate(cases, rows)
    assert m["by_category"]["natural_language_only"]["hit5_passage"] == 1.0
    assert m["by_category"]["doc_to_code"]["hit5_passage"] == 0.0
    assert m["by_language"]["ja"]["cases"] == 1 and m["by_language"]["en"]["hit5_passage"] == 0.0


def test_percentile_and_acceptable():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 100], 95) == 100
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([], 95) is None
    assert acceptable(0.8, 0.8) is True and acceptable(0.79, 0.8) is False
    assert acceptable("Acceptable", 0.8) is True and acceptable("unacceptable", 0.8) is False
    assert acceptable(None, 0.8) is None and acceptable("maybe", 0.8) is None


# --- しきい値 ---
def entry(method, hit5, rate=None, p95=None, time=None, tokens=None, run="r1"):
    return {"run_id": run, "method": method, "agent": method.startswith("agent_"),
            "retrieval": {"hit5_passage": hit5}, "answers": {"acceptable_rate": rate},
            "search_time_ms": {"p95": p95}, "task_time_seconds_median": time,
            "cost": {"ai_units_median": None, "cost_usd_median": None, "total_tokens_median": tokens},
            "by_category": {}}


def test_absolute_thresholds():
    th = DEFAULT_THRESHOLDS
    assert judge(entry("hybrid", 0.85, p95=120.0), None, th)["overall"] == "pass"
    assert judge(entry("keyword", 0.50, p95=5.0), None, th)["overall"] == "fail"
    assert judge(entry("hybrid", 0.85, p95=3500.0), None, th)["overall"] == "fail"
    # エージェントを使う手法は、回答を採点するまで「未確定」
    assert judge(entry("agent_cf", 0.9), None, th)["overall"] == "incomplete"
    assert judge(entry("agent_cf", 0.9, rate=0.7), None, th)["overall"] == "fail"


def test_relative_threshold_needs_quality_kept_and_time_or_cost_down():
    th = DEFAULT_THRESHOLDS
    base = entry("agent_grep", 0.85, rate=0.85, time=100.0, tokens=50_000)
    faster = judge(entry("agent_cf", 0.9, rate=0.85, time=75.0, tokens=60_000), base, th)
    assert faster["relative"]["verdict"] == "pass" and faster["overall"] == "pass"
    cheaper = judge(entry("agent_cf", 0.9, rate=0.9, time=99.0, tokens=30_000), base, th)
    assert cheaper["relative"]["cost_measure"] == "tokens" and cheaper["relative"]["verdict"] == "pass"
    same = judge(entry("agent_cf", 0.9, rate=0.9, time=95.0, tokens=48_000), base, th)
    assert same["relative"]["verdict"] == "fail" and same["overall"] == "fail"
    worse = judge(entry("agent_cf", 0.9, rate=0.80, time=50.0, tokens=10_000), base, th)
    assert worse["relative"]["quality_not_lower"] is False and worse["relative"]["verdict"] == "fail"


def test_summary_notes_when_baseline_already_meets_absolute_thresholds():
    cases = {"Q1": case()}
    base = entry("agent_grep", 0.9, rate=0.9, time=100.0, tokens=1000)
    base["by_category"] = {"natural_language_only": {"hit5_passage": 0.5}, "identifier_known": {"hit5_passage": 1.0}}
    poc = entry("agent_cf", 0.95, rate=0.9, time=70.0, tokens=900)
    poc["by_category"] = {"natural_language_only": {"hit5_passage": 0.9}, "identifier_known": {"hit5_passage": 1.0}}
    summary = build_summary(cases, [base, poc], [], "agent_grep")
    assert summary["baseline_note"]["baseline_meets_absolute"] is True
    assert summary["baseline_note"]["categories_recovered"] == {"agent_cf [r1]": ["natural_language_only"]}


# --- データの検査 ---
def test_fixture_cases_are_valid():
    cases, issues = load_cases(CASES)
    assert len(cases) == 17 and not [i for i in issues if i.level in ("error", "warning")]


def test_validate_reports_line_numbers(tmp_path, capsys):
    good = case("A1")
    lines = [json.dumps(good), "{not json", json.dumps(dict(good, case_id="A1")),
             json.dumps(dict(case("A2"), category="typo")),
             json.dumps(dict(case("A3", evidence=[ev("a.cpp", 9, 3)]))),
             json.dumps(dict(case("A4", evidence=[], category="doc_to_code"))),
             json.dumps(dict(case("A5", category="unanswerable"))),
             json.dumps({k: v for k, v in case("A6").items() if k != "question_language"}),
             json.dumps(dict(case("A7"), extra_field=1))]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc = validate_main(Namespace(cases=str(path), results=[], runs=[], check_corpus=False, config=None))
    out = capsys.readouterr().out
    assert rc == 1
    assert "cases.jsonl:2: [誤り] JSON として読めません" in out
    assert "cases.jsonl:3: [誤り] case_id A1 が 1 行目と重なっています" in out
    assert "cases.jsonl:4: [誤り] category: 'typo' は使えません" in out
    assert "cases.jsonl:5: [誤り] expected_evidence/0: end_line(3)が start_line(9)より前です" in out
    assert "cases.jsonl:6: [誤り] expected_evidence: 少なくとも 1 件が要ります" in out
    assert "cases.jsonl:7: [誤り] expected_evidence: 0 件までです" in out
    assert "cases.jsonl:8: [誤り] (行全体): 必須の項目がありません: question_language" in out
    assert "cases.jsonl:9: [誤り] (行全体): 知らない項目があります: extra_field" in out


def test_validate_results_cross_checks(tmp_path):
    rows = [result("Q1", "keyword", [ev("a.cpp", 1, 2, rank=1)]),
            result("Q9", "keyword", []),                                           # 質問が無い
            result("Q1", "keyword", [ev("a.cpp", 1, 2, rank=2)], trial=2),         # rank が 1 から始まらない
            result("Q1", "agent_grep", [], answer=None),                           # 完了なのに回答が無い
            dict(result("Q1", "semantic", []), search_time_ms=None)]               # 検索時間が無い
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    records, issues = read_jsonl(path, "results")
    assert len(records) == 5 and not issues
    text = "\n".join(i.format() for i in check_results(records, "results.jsonl", {"Q1"}))
    assert "results.jsonl:2: [誤り] case_id Q9 が cases.jsonl にありません" in text
    assert "results.jsonl:3: [誤り] retrieved_evidence の rank は 1 から順に並べてください" in text
    assert "results.jsonl:4: [誤り] エージェントを使う手法の結果には answer が要ります" in text
    assert "results.jsonl:5: [誤り] 検索だけの手法の結果には search_time_ms が要ります" in text
    assert "agent_grep: 1 件の質問で回数が 3 回に足りません" in text


def test_check_cases_warns_about_thin_categories():
    records = [(i + 1, case(f"Q{i}")) for i in range(5)]
    text = "\n".join(i.format() for i in check_cases(records, "cases.jsonl"))
    assert "category doc_to_code が 0 件です" in text and "質問は 5 件です" in text


def test_validate_check_corpus(indexed_workspace, tmp_path, capsys):
    bad = case("B1", evidence=[ev("Core/JobScheduler.cpp", 60, 999), ev("Core/Missing.cpp", 1, 2),
                               ev("a.cpp", 1, 2, source="nope")])
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    rc = validate_main(Namespace(cases=str(path), results=[], runs=[], check_corpus=True,
                                 config=str(indexed_workspace)))
    out = capsys.readouterr().out
    assert rc == 1 and "end_line 999 がファイルの行数 62 を超えています" in out
    assert "ファイルがありません: engine:Core/Missing.cpp" in out and "source_id nope が設定にありません" in out
    rc = validate_main(Namespace(cases=str(CASES), results=[], runs=[], check_corpus=True,
                                 config=str(indexed_workspace)))
    assert rc == 0


# --- 実行と取り込み ---
def eval_args(ws, out, **kw):
    base = dict(config=str(ws), cases=str(CASES), out=str(out), methods="keyword,semantic,hybrid", top_k=10,
                imports=[], import_runs=[], baseline="agent_grep", run_id="fixture-run", corpus_revision="fixture",
                candidate="hybrid,agent_cf", split="eval")
    base.update(kw)
    return Namespace(**base)


def test_eval_runs_three_methods_and_writes_valid_files(indexed_workspace, tmp_path, capsys):
    out = tmp_path / "eval"
    assert eval_main(eval_args(indexed_workspace, out)) == 0
    records, issues = read_jsonl(out / "results.jsonl", "results")
    assert not issues and len(records) == 17 * 3
    assert not [i for i in check_results(records, "results.jsonl", set(load_cases(CASES)[0])) if i.level == "error"]
    runs, issues = read_jsonl(out / "runs.jsonl", "runs")
    assert not issues and runs[0][1]["corpus"]["files"] == 13
    run = runs[0][1]
    assert run["embedding"]["dimensions"] == 256 and run["index"]["size_bytes"] > 0
    assert "root" not in json.dumps(run)          # 絶対パスを実行の情報に入れない
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    by_method = {m["method"]: m for m in summary["methods"]}
    assert set(by_method) == {"keyword", "semantic", "hybrid"}
    kw = by_method["keyword"]
    assert kw["retrieval"]["cases"] == 15 and kw["unanswerable_cases"] == 2
    assert kw["by_category"]["identifier_known"]["hit5_passage"] == 1.0      # 識別子が分かっている質問は keyword で当たる
    assert kw["search_time_ms"]["p95"] is not None and kw["judgement"]["overall"] in ("pass", "fail")
    assert set(kw["by_language"]) == {"ja", "en"}
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## 手法ごとの指標" in md and "| keyword | fixture-run |" in md and "## しきい値の合否" in md
    assert "identifier_known" in md and "| hybrid | fixture-run | ja |" in md


def test_eval_imports_other_results_into_the_same_summary(indexed_workspace, tmp_path):
    cases, _ = load_cases(CASES)
    rows = []
    for method, seconds, tokens in (("agent_grep", 60.0, 40_000), ("agent_cf", 30.0, 20_000)):
        for cid, c in cases.items():
            for trial in (1, 2, 3):
                found = [dict(rank=1, source_id=e["source_id"], path=e["path"], start_line=e["start_line"],
                              end_line=e["end_line"], origin="cited") for e in c["expected_evidence"][:1]]
                rows.append(result(cid, method, found, trial, run="agent-run", answer="...", answer_correctness=1.0,
                                   task_time_seconds=seconds, input_tokens=tokens, output_tokens=500, tool_calls=4))
    imported = tmp_path / "agent-results.jsonl"
    imported.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    runs = tmp_path / "agent-runs.jsonl"
    runs.write_text(json.dumps({"run_id": "agent-run", "created_at": "2026-01-01T00:00:00+0900",
                                "corpus_revision": "fixture", "methods": {"agent_grep": {}, "agent_cf": {}},
                                "agent": {"name": "copilot-cli", "version": "1.0", "model": "m", "trials": 3}}) + "\n",
                    encoding="utf-8")
    out = tmp_path / "eval"
    assert eval_main(eval_args(indexed_workspace, out, imports=[str(imported)], import_runs=[str(runs)])) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    by_method = {m["method"]: m for m in summary["methods"]}
    assert set(by_method) == {"keyword", "semantic", "hybrid", "agent_grep", "agent_cf"}
    rel = by_method["agent_cf"]["judgement"]["relative"]
    assert rel["verdict"] == "pass" and rel["time_reduction"] == pytest.approx(0.5)
    assert by_method["agent_grep"]["judgement"]["relative"] is None
    assert {r["run_id"] for r in summary["runs"]} == {"fixture-run", "agent-run"}
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## エージェントを使う手法の回答・時間・費用" in md and "既存の手順が、すでに絶対の条件を満たしています" in md
    # 取り込みだけでも要約を作れる
    out2 = tmp_path / "eval2"
    assert eval_main(eval_args(indexed_workspace, out2, methods="none", imports=[str(imported)])) == 0
    assert not (out2 / "results.jsonl").exists() and (out2 / "summary.md").exists()


def test_eval_keeps_tuning_questions_out_of_the_decision(indexed_workspace, tmp_path):
    """調整用(split が tune)の質問は、既定では判定に使わない。"""
    cases, _ = load_cases(CASES)
    rows = [dict(c, split="tune") if cid in ("F001", "F002", "F016") else c for cid, c in cases.items()]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "eval"
    assert eval_main(eval_args(indexed_workspace, out, cases=str(path), methods="keyword")) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases"]["total"] == 14 and summary["methods"][0]["retrieval"]["cases"] == 13
    out = tmp_path / "tune"
    assert eval_main(eval_args(indexed_workspace, out, cases=str(path), methods="keyword", split="tune")) == 0
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["cases"]["total"] == 3


def test_eval_marks_the_methods_chosen_before_the_run(indexed_workspace, tmp_path):
    out = tmp_path / "eval"
    assert eval_main(eval_args(indexed_workspace, out, candidate="hybrid")) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["candidate_methods"] == ["hybrid"]
    assert {m["method"]: m["candidate"] for m in summary["methods"]} == {
        "hybrid": True, "keyword": False, "semantic": False}
    run = json.loads((out / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert run["candidate_methods"] == ["hybrid"] and run["index"]["update_mode"] == "full_rebuild"
    assert run["host"]["cpu_count"] >= 1 and "hostname" not in json.dumps(run).lower()
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["returned_bytes"] > 0 for r in rows)
    assert "| hybrid | fixture-run | ○ |" in (out / "summary.md").read_text(encoding="utf-8")


def test_eval_rejects_invalid_cases(indexed_workspace, tmp_path, capsys):
    bad = tmp_path / "cases.jsonl"
    bad.write_text(json.dumps(dict(case(), category="typo")) + "\n", encoding="utf-8")
    assert eval_main(eval_args(indexed_workspace, tmp_path / "out", cases=str(bad))) == 1
    assert "category" in capsys.readouterr().err
