"""一致の判定と指標。定義は docs/evaluation-data-spec.md の「Matching and metrics」と同じ。

- passage の一致: source_id と path が同じで、行の範囲が 1 行以上重なる
- ファイルの一致: source_id と path が同じ
- 指標: hit@1・5・10(passage とファイル)、MRR、expected_evidence のうち見つかった割合
- まとめ方: 質問ごとに回(trial)の平均を取り、それを質問で平均する
"""
from __future__ import annotations

import math
from collections import defaultdict

from .data import is_agent_method

HIT_KS = (1, 5, 10)
RECALL_K = 10
ACCEPT_WORDS = {"acceptable", "correct", "pass", "ok", "yes"}
REJECT_WORDS = {"unacceptable", "incorrect", "fail", "no"}


def norm_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.casefold()


def same_file(a: dict, b: dict) -> bool:
    return a["source_id"] == b["source_id"] and norm_path(a["path"]) == norm_path(b["path"])


def passage_match(a: dict, b: dict) -> bool:
    return same_file(a, b) and a["start_line"] <= b["end_line"] and b["start_line"] <= a["end_line"]


def score_result(case: dict, result: dict) -> dict:
    """1 つの結果を採点する。答えの無い質問(expected_evidence が空)は answerable=False。"""
    expected = case["expected_evidence"]
    if not expected:
        return {"answerable": False}
    retrieved = sorted(result["retrieved_evidence"], key=lambda e: e["rank"])
    passage_rank = file_rank = None
    found: set[int] = set()
    for e in retrieved:
        for i, ev in enumerate(expected):
            if same_file(e, ev):
                if file_rank is None:
                    file_rank = e["rank"]
                if passage_match(e, ev):
                    if passage_rank is None:
                        passage_rank = e["rank"]
                    if e["rank"] <= RECALL_K:
                        found.add(i)
    out = {"answerable": True, "passage_rank": passage_rank, "file_rank": file_rank,
           "evidence_recall": len(found) / len(expected),
           "rr_passage": 1.0 / passage_rank if passage_rank else 0.0,
           "rr_file": 1.0 / file_rank if file_rank else 0.0}
    for k in HIT_KS:
        out[f"hit{k}_passage"] = 1.0 if passage_rank and passage_rank <= k else 0.0
        out[f"hit{k}_file"] = 1.0 if file_rank and file_rank <= k else 0.0
    return out


def acceptable(value, acceptable_min: float) -> bool | None:
    """answer_correctness が合格か。採点していなければ None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value >= acceptable_min
    word = str(value).strip().lower()
    if word in ACCEPT_WORDS:
        return True
    if word in REJECT_WORDS:
        return False
    return None


def percentile(values: list[float], p: float) -> float | None:
    """最近傍順位法の百分位(p は 0〜100)。"""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


RETRIEVAL_KEYS = ([f"hit{k}_passage" for k in HIT_KS] + [f"hit{k}_file" for k in HIT_KS]
                  + ["rr_passage", "rr_file", "evidence_recall"])


def _retrieval_block(per_case: dict[str, list[dict]]) -> dict:
    """質問ごとに回の平均を取り、質問で平均する。per_case は case_id → 採点の並び(answerable のものだけ)。"""
    out: dict = {"cases": len(per_case)}
    for key in RETRIEVAL_KEYS:
        vals = [_mean([s[key] for s in scores]) for scores in per_case.values() if scores]
        out[key] = _mean([v for v in vals if v is not None])
    return out


def _answer_block(per_case: dict[str, list[bool]]) -> dict:
    vals = [_mean([1.0 if a else 0.0 for a in marks]) for marks in per_case.values() if marks]
    return {"scored_cases": len(vals), "acceptable_rate": _mean([v for v in vals if v is not None])}


def aggregate(cases: dict[str, dict], results: list[dict], acceptable_min: float = 0.8) -> list[dict]:
    """(run_id, method) ごとの指標を返す。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        if r["case_id"] in cases:
            groups[(r["run_id"], r["method"])].append(r)

    out: list[dict] = []
    for (run_id, method), rows in sorted(groups.items()):
        scored: dict[str, list[dict]] = defaultdict(list)
        marks: dict[str, list[bool]] = defaultdict(list)
        by_cat: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        by_lang: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        cat_marks: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
        lang_marks: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
        status = defaultdict(int)
        unanswerable_cases: set[str] = set()
        for r in rows:
            case = cases[r["case_id"]]
            status[r["status"]] += 1
            s = score_result(case, r)
            if s["answerable"]:
                scored[r["case_id"]].append(s)
                by_cat[case["category"]][r["case_id"]].append(s)
                by_lang[case["question_language"]][r["case_id"]].append(s)
            else:
                unanswerable_cases.add(r["case_id"])
            ok = acceptable(r.get("answer_correctness"), acceptable_min)
            if ok is not None:
                marks[r["case_id"]].append(ok)
                cat_marks[case["category"]][r["case_id"]].append(ok)
                lang_marks[case["question_language"]][r["case_id"]].append(ok)

        def nums(key: str) -> list[float]:
            return [float(r[key]) for r in rows if r.get(key) is not None]

        latency = nums("search_time_ms")
        tokens = [float((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)) for r in rows
                  if r.get("input_tokens") is not None or r.get("output_tokens") is not None]
        entry = {
            "run_id": run_id, "method": method, "agent": is_agent_method(method),
            "results": len(rows), "trials": max(r["trial"] for r in rows),
            "status": dict(status), "unanswerable_cases": len(unanswerable_cases),
            "retrieval": _retrieval_block(scored),
            "answers": _answer_block(marks),
            "search_time_ms": {"p50": percentile(latency, 50), "p95": percentile(latency, 95), "n": len(latency)},
            "task_time_seconds_median": median(nums("task_time_seconds")),
            "cost": {
                "input_tokens_median": median(nums("input_tokens")),
                "output_tokens_median": median(nums("output_tokens")),
                "total_tokens_median": median(tokens),
                "ai_units_median": median(nums("ai_units")),
                "cost_usd_median": median(nums("cost_usd")),
                "tool_calls_median": median(nums("tool_calls")),
                "model_calls_median": median(nums("model_calls")),
            },
            # モデルに渡した道具の出力の大きさと、大きすぎて渡らなかった(ファイルに退かされた)結果の数
            "returned_bytes_median": median(nums("returned_bytes")),
            "offloaded_results": int(sum(nums("offloaded_results"))),
            # 索引の後に変わったファイルを指していた結果の数(鮮度)
            "stale_evidence": sum(1 for r in rows for e in r["retrieved_evidence"] if e.get("stale")),
            "by_category": {c: dict(_retrieval_block(v), **_answer_block(cat_marks.get(c, {})))
                            for c, v in sorted(by_cat.items())},
            "by_language": {c: dict(_retrieval_block(v), **_answer_block(lang_marks.get(c, {})))
                            for c, v in sorted(by_lang.items())},
        }
        # 答えの無い質問は、検索の指標には入らないので、回答の合格率だけを別に出す
        un = {cid: m for cid, m in marks.items() if cases[cid]["category"] == "unanswerable"}
        entry["unanswerable_answers"] = _answer_block(un)
        out.append(entry)
    return out
